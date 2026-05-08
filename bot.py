# bot.py - PRIMEIRA LINHA
import sys
if sys.version_info >= (3, 13):
    import patch

import discord
from discord.ext import commands, tasks
from discord import Embed, Color
import aiohttp
import mercadopago
import uuid
import asyncio
import os
import asyncpg
import hashlib
import hmac
from datetime import datetime, timezone, timedelta
from aiohttp import web

# ================= CONFIG =================
CARGO_DONO    = int(os.getenv("CARGO_DONO", "0"))
CANAL_LOJA    = int(os.getenv("CANAL_LOJA", "0"))
CANAL_VENDAS  = int(os.getenv("CANAL_VENDAS", "0"))
CANAL_FALHAS  = int(os.getenv("CANAL_FALHAS", "0"))
WEBHOOK_LOG   = os.getenv("WEBHOOK_LOG", "")
DISCORD_TOKEN = os.getenv("LOJA_DISCORD_TOKEN")
MP_TOKEN      = os.getenv("MERCADO_PAGO_TOKEN")
MP_SECRET     = os.getenv("MP_WEBHOOK_SECRET", "")
DATABASE_URL  = os.getenv("DATABASE_URL")

for nome, val in [("LOJA_DISCORD_TOKEN", DISCORD_TOKEN),
                  ("MERCADO_PAGO_TOKEN", MP_TOKEN),
                  ("DATABASE_URL", DATABASE_URL)]:
    if not val:
        print(f"❌ ERRO: {nome} não configurado!")
        exit(1)

sdk     = mercadopago.SDK(MP_TOKEN)
intents = discord.Intents.all()
bot     = commands.Bot(command_prefix="!", intents=intents)

db_pool: asyncpg.Pool = None
pedidos_pendentes: dict = {}

# Cooldown por usuário
cooldowns: dict = {}
COOLDOWN_SEGUNDOS = 60

# ================= BANCO DE DADOS =================
SCHEMA_VERSION = 3

async def init_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            # Tabela schema_version
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    versao INTEGER PRIMARY KEY
                )
            """)

            # Tabela produtos
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS produtos (
                    id          TEXT PRIMARY KEY,
                    nome        TEXT NOT NULL,
                    preco       NUMERIC(10,2) NOT NULL,
                    emoji       TEXT DEFAULT '🛒',
                    link        TEXT NOT NULL,
                    estoque     INTEGER DEFAULT -1,
                    vendas      INTEGER DEFAULT 0,
                    criado_em   TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Tabela pedidos
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pedidos (
                    id              TEXT PRIMARY KEY,
                    user_id         BIGINT NOT NULL,
                    user_tag        TEXT,
                    produto_id      TEXT NOT NULL,
                    produto_nome    TEXT NOT NULL,
                    produto_preco   NUMERIC(10,2) NOT NULL,
                    status          TEXT DEFAULT 'pendente',
                    entregue        BOOLEAN DEFAULT FALSE,
                    tentativas      INTEGER DEFAULT 0,
                    criado_em       TIMESTAMPTZ DEFAULT NOW(),
                    atualizado_em   TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Tabela estatisticas
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS estatisticas (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                )
            """)

            # Tabela painel_ids
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS painel_ids (
                    nome   TEXT PRIMARY KEY,
                    msg_id BIGINT NOT NULL
                )
            """)

            # Inserir estatísticas iniciais
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('vendas','0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('faturamento','0.0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('vendas_hoje','0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('faturamento_hoje','0.0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('ultima_reset','') ON CONFLICT (chave) DO NOTHING")
            
            # Verificar versão do schema
            row = await conn.fetchrow("SELECT versao FROM schema_version LIMIT 1")
            versao_atual = row["versao"] if row else 0

            if versao_atual < 1:
                await conn.execute("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS estoque INTEGER DEFAULT -1")
                await conn.execute("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS vendas INTEGER DEFAULT 0")
            if versao_atual < 2:
                await conn.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tentativas INTEGER DEFAULT 0")
            if versao_atual < 3:
                await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('vendas_hoje','0') ON CONFLICT (chave) DO NOTHING")
                await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('faturamento_hoje','0.0') ON CONFLICT (chave) DO NOTHING")
                await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('ultima_reset','') ON CONFLICT (chave) DO NOTHING")

            await conn.execute("""
                INSERT INTO schema_version (versao) VALUES ($1)
                ON CONFLICT (versao) DO UPDATE SET versao=$1
            """, SCHEMA_VERSION)

        print(f"✅ Banco de dados inicializado (schema v{SCHEMA_VERSION}).")
    except Exception as e:
        print(f"❌ Erro ao conectar ao banco: {e}")
        db_pool = None

async def db_listar_produtos() -> dict:
    if not db_pool:
        return {}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM produtos ORDER BY criado_em")
    return {r["id"]: dict(r) for r in rows}

async def db_adicionar_produto(pid, nome, preco, emoji, link, estoque):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO produtos (id,nome,preco,emoji,link,estoque) VALUES ($1,$2,$3,$4,$5,$6)",
            pid, nome, preco, emoji, link, estoque
        )

async def db_editar_produto(pid, nome, preco, estoque):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE produtos SET nome=$2, preco=$3, estoque=$4 WHERE id=$1",
            pid, nome, preco, estoque
        )

async def db_remover_produto(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM produtos WHERE id=$1", pid)

async def db_produto_mais_vendido() -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM produtos ORDER BY vendas DESC LIMIT 1")
    return dict(row) if row else None

async def db_decrementar_estoque(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE produtos SET estoque = estoque - 1, vendas = vendas + 1
            WHERE id=$1 AND estoque > 0
        """, pid)

async def db_verificar_estoque(pid) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT estoque FROM produtos WHERE id=$1", pid)
    if not row:
        return False
    return row["estoque"] == -1 or row["estoque"] > 0

async def db_inserir_pedido(pid, user_id, user_tag, produto_id, produto_nome, produto_preco):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO pedidos (id,user_id,user_tag,produto_id,produto_nome,produto_preco,status,entregue,tentativas)
            VALUES ($1,$2,$3,$4,$5,$6,'pendente',FALSE,0)
        """, pid, user_id, user_tag, produto_id, produto_nome, produto_preco)

async def db_buscar_pedido(pid) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM pedidos WHERE id=$1", pid)
    return dict(row) if row else None

async def db_marcar_entregue(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE pedidos SET status='aprovado', entregue=TRUE,
            tentativas=tentativas+1, atualizado_em=NOW() WHERE id=$1
        """, pid)

async def db_marcar_falha_entrega(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE pedidos SET status='falha_entrega',
            tentativas=tentativas+1, atualizado_em=NOW() WHERE id=$1
        """, pid)

async def db_marcar_expirado(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE pedidos SET status='expirado', atualizado_em=NOW() WHERE id=$1
        """, pid)

async def db_pedidos_usuario(user_id: int) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM pedidos WHERE user_id=$1 ORDER BY criado_em DESC LIMIT 10
        """, user_id)
    return [dict(r) for r in rows]

async def db_pedidos_falha_pendentes() -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM pedidos WHERE status='falha_entrega' ORDER BY criado_em DESC
        """)
    return [dict(r) for r in rows]

async def db_pedidos_pendentes_antigos(minutos: int = 35) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM pedidos
            WHERE status='pendente'
            AND criado_em < NOW() - ($1 * INTERVAL '1 minute')
        """, minutos)
    return [dict(r) for r in rows]

async def db_get_stat(chave: str) -> float:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT valor FROM estatisticas WHERE chave=$1", chave)
    return float(row["valor"]) if row and row["valor"] else 0.0

async def db_get_stat_str(chave: str) -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT valor FROM estatisticas WHERE chave=$1", chave)
    return row["valor"] if row else ""

async def db_set_stat(chave: str, valor: str):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO estatisticas (chave, valor) VALUES ($1,$2)
            ON CONFLICT (chave) DO UPDATE SET valor=$2
        """, chave, valor)

async def db_incrementar_venda(preco: float):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE estatisticas SET valor=(valor::NUMERIC+1)::TEXT WHERE chave='vendas'")
        await conn.execute("UPDATE estatisticas SET valor=(valor::NUMERIC+$1)::TEXT WHERE chave='faturamento'", preco)
        await conn.execute("UPDATE estatisticas SET valor=(valor::NUMERIC+1)::TEXT WHERE chave='vendas_hoje'")
        await conn.execute("UPDATE estatisticas SET valor=(valor::NUMERIC+$1)::TEXT WHERE chave='faturamento_hoje'", preco)

async def db_reset_stats_diarias():
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE estatisticas SET valor='0' WHERE chave='vendas_hoje'")
        await conn.execute("UPDATE estatisticas SET valor='0.0' WHERE chave='faturamento_hoje'")
        await conn.execute("UPDATE estatisticas SET valor=$1 WHERE chave='ultima_reset'", 
                          datetime.now(timezone.utc).isoformat())

async def db_get_painel_id(nome: str) -> int | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT msg_id FROM painel_ids WHERE nome=$1", nome)
    return row["msg_id"] if row else None

async def db_set_painel_id(nome: str, msg_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO painel_ids (nome,msg_id) VALUES ($1,$2)
            ON CONFLICT (nome) DO UPDATE SET msg_id=$2
        """, nome, msg_id)

def formatar_preco(valor):
    valor = float(valor)
    if float(valor) == int(valor):
        return str(int(valor))
    return f"{valor:.2f}".rstrip("0").rstrip(".").replace(".", ",")

def eh_dono(interaction: discord.Interaction) -> bool:
    return any(r.id == CARGO_DONO for r in interaction.user.roles)

def status_emoji(status: str) -> str:
    return {"pendente":"🟡","aprovado":"🟢","falha_entrega":"🔴","expirado":"⚫"}.get(status, "⚪")

def verificar_assinatura_mp(payload: bytes, header_signature: str, secret: str) -> bool:
    if not secret or not header_signature:
        return True
    try:
        partes = dict(p.split("=", 1) for p in header_signature.split(","))
        ts   = partes.get("ts", "")
        v1   = partes.get("v1", "")
        msg  = f"{ts}.{payload.decode('utf-8')}"
        calc = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc, v1)
    except Exception:
        return False

def verificar_cooldown(user_id: int) -> int:
    ultimo = cooldowns.get(user_id)
    if not ultimo:
        return 0
    restante = COOLDOWN_SEGUNDOS - (datetime.now(timezone.utc) - ultimo).total_seconds()
    return max(0, int(restante))

def registrar_cooldown(user_id: int):
    cooldowns[user_id] = datetime.now(timezone.utc)

# ================= EMBEDS =================
async def montar_embed_vendas():
    vendas           = await db_get_stat("vendas")
    faturamento      = await db_get_stat("faturamento")
    vendas_hoje      = await db_get_stat("vendas_hoje")
    fat_hoje         = await db_get_stat("faturamento_hoje")
    mais_vendido     = await db_produto_mais_vendido()
    falhas           = await db_pedidos_falha_pendentes()

    embed = Embed(title="📊 PAINEL DE VENDAS", color=Color.dark_gold(),
                  timestamp=datetime.now(timezone.utc))
    embed.add_field(name="📦 Vendas Totais",   value=str(int(vendas)),                   inline=True)
    embed.add_field(name="💰 Faturamento Total",value=f"R$ {formatar_preco(faturamento)}", inline=True)
    embed.add_field(name="\u200b",             value="\u200b",                           inline=True)
    embed.add_field(name="📅 Vendas Hoje",      value=str(int(vendas_hoje)),              inline=True)
    embed.add_field(name="💵 Faturamento Hoje", value=f"R$ {formatar_preco(fat_hoje)}",   inline=True)
    embed.add_field(name="\u200b",             value="\u200b",                           inline=True)

    if mais_vendido:
        embed.add_field(
            name="🏆 Produto Mais Vendido",
            value=f"{mais_vendido.get('emoji','🛒')} {mais_vendido['nome']} ({mais_vendido['vendas']} vendas)",
            inline=False
        )
    if falhas:
        embed.add_field(
            name="⚠️ Falhas de Entrega Pendentes",
            value=f"{len(falhas)} pedido(s) não entregue(s)",
            inline=False
        )
    embed.set_footer(text="Atualizado automaticamente a cada 2 min")
    return embed

async def montar_embed_loja():
    produtos = await db_listar_produtos()
    
    # Embed principal com visual bonito
    embed = Embed(
        title="✨ **NEXZY STORE** ✨",
        description="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   "**🎉 A MELHOR EXPERIÊNCIA DE COMPRAS 🎉**\n"
                   "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                   "```fix\n✔️ Pagamento via PIX (Instantâneo)\n✔️ Entrega automática na DM\n✔️ Suporte 24/7\n✔️ 100% Seguro e Confiável```\n"
                   "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        color=0x5865F2
    )
    
    # Banner
    embed.set_image(url="https://media.discordapp.net/attachments/1491808878562643998/1491808965170958396/e6876514-c5ae-477f-a84b-d7b7db0c01e5.png")
    
    # Footer
    embed.set_footer(text="⭐ Nexzy Store • A Loja Oficial ⭐", icon_url=bot.user.avatar.url if bot.user.avatar else None)
    
    embed.timestamp = datetime.now(timezone.utc)
    
    if not produtos:
        embed.add_field(
            name="📢 **SEM PRODUTOS**",
            value="```diff\n- Nenhum produto cadastrado ainda!\n+ Aguarde novidades em breve...```",
            inline=False
        )
        return embed
    
    # Lista de produtos em formato elegante
    for pid, prod in produtos.items():
        estoque_texto = "∞" if prod["estoque"] == -1 else str(prod["estoque"])
        
        value = (
            f"```ml\n"
            f"💰 Preço: R$ {formatar_preco(prod['preco'])}\n"
            f"📦 Estoque: {estoque_texto}\n"
            f"🆔 ID: {pid}\n"
            f"```"
        )
        
        embed.add_field(
            name=f"{prod.get('emoji', '🛒')} **{prod['nome']}**",
            value=value,
            inline=True
        )
    
    # Guia rápido
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━",
        value="**📌 COMO COMPRAR?**\n"
              "```\n1️⃣ Clique no botão COMPRAR\n2️⃣ Escolha seu produto\n3️⃣ Efetue o PIX\n4️⃣ Receba o link na DM```"
              "\n💬 **Precisa de ajuda?** Contate um administrador!",
        inline=False
    )
    
    return embed

# ================= VIEWS =================
class SelecionarProduto(discord.ui.Select):
    def __init__(self, produtos: dict):
        options = []
        for pid, prod in produtos.items():
            estoque_ok = prod["estoque"] == -1 or prod["estoque"] > 0
            if not estoque_ok:
                continue
            options.append(
                discord.SelectOption(
                    label=f"{prod['nome']} - R$ {formatar_preco(prod['preco'])}",
                    value=pid,
                    emoji=prod.get('emoji', '🛒'),
                    description=f"ID: {pid}"
                )
            )
        super().__init__(placeholder="Selecione um produto...", options=options[:25])
    
    async def callback(self, interaction: discord.Interaction):
        await processar_compra(interaction, self.values[0])

class PainelPrincipal(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="💰 Comprar", style=discord.ButtonStyle.success, custom_id="btn_comprar")
    async def btn_comprar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db_listar_produtos()
        disponiveis = {k: v for k, v in produtos.items() if v["estoque"] != 0}
        if not disponiveis:
            return await interaction.response.send_message("❌ Nenhum produto disponível no momento.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(SelecionarProduto(disponiveis))
        await interaction.response.send_message("Selecione o produto desejado:", view=view, ephemeral=True)
    
    @discord.ui.button(label="📜 Meus Pedidos", style=discord.ButtonStyle.secondary, custom_id="btn_pedidos")
    async def btn_pedidos(self, interaction: discord.Interaction, button: discord.ui.Button):
        pedidos = await db_pedidos_usuario(interaction.user.id)
        if not pedidos:
            return await interaction.response.send_message("📭 Você não tem nenhum pedido.", ephemeral=True)
        embed = Embed(title="📜 Seus Pedidos", color=Color.blue())
        for p in pedidos[:10]:
            embed.add_field(name=f"{status_emoji(p['status'])} {p['produto_nome']}", value=f"ID: `{p['id']}`\nValor: R$ {formatar_preco(p['produto_preco'])}\nData: {p['criado_em'].strftime('%d/%m/%Y %H:%M')}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="👑 Admin", style=discord.ButtonStyle.danger, custom_id="btn_admin", row=1)
    async def btn_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_dono(interaction):
            return await interaction.response.send_message("❌ Apenas administradores podem acessar.", ephemeral=True)
        await interaction.response.send_message(embed=await montar_embed_admin(), view=AdminView(), ephemeral=True)

class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @discord.ui.button(label="➕ Adicionar Produto", style=discord.ButtonStyle.success)
    async def add_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(AdicionarProdutoModal())
        except Exception as e:
            print(f"Erro ao abrir modal: {e}")
            await interaction.response.send_message("❌ Erro ao abrir formulário. Tente novamente.", ephemeral=True)
    
    @discord.ui.button(label="✏️ Editar Produto", style=discord.ButtonStyle.primary)
    async def edit_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db_listar_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder="Selecione um produto para editar...")
        for pid, prod in produtos.items():
            select.add_option(label=f"{prod['nome']} - R$ {formatar_preco(prod['preco'])}", value=pid, emoji=prod.get('emoji', '🛒'))
        
        async def select_callback(interaction: discord.Interaction):
            await interaction.response.send_modal(EditarProdutoModal(select.values[0]))
        
        select.callback = select_callback
        view.add_item(select)
        await interaction.response.send_message("Selecione o produto:", view=view, ephemeral=True)
    
    @discord.ui.button(label="🗑️ Remover Produto", style=discord.ButtonStyle.danger)
    async def remove_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db_listar_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        
        view = discord.ui.View(timeout=60)
        select = discord.ui.Select(placeholder="Selecione um produto para remover...")
        for pid, prod in produtos.items():
            select.add_option(label=f"{prod['nome']} - R$ {formatar_preco(prod['preco'])}", value=pid, emoji=prod.get('emoji', '🛒'))
        
        async def select_callback(interaction: discord.Interaction):
            await db_remover_produto(select.values[0])
            await interaction.response.send_message("✅ Produto removido com sucesso!", ephemeral=True)
            await atualizar_painel_loja()
            await atualizar_painel_vendas()
        
        select.callback = select_callback
        view.add_item(select)
        await interaction.response.send_message("Selecione o produto para remover:", view=view, ephemeral=True)
    
    @discord.ui.button(label="📊 Estatísticas", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await montar_embed_vendas()
        await interaction.response.send_message(embed=embed, ephemeral=True)

class AdicionarProdutoModal(discord.ui.Modal, title="Adicionar Produto"):
    pid = discord.ui.TextInput(label="ID do Produto", placeholder="ex: produto1", required=True)
    nome = discord.ui.TextInput(label="Nome", placeholder="Nome do produto", required=True)
    preco = discord.ui.TextInput(label="Preço", placeholder="19.90", required=True)
    emoji = discord.ui.TextInput(label="Emoji", placeholder="🛒", required=False, default="🛒")
    link = discord.ui.TextInput(label="Link de Entrega", placeholder="https://...", required=True)
    estoque = discord.ui.TextInput(label="Estoque (-1 = Ilimitado)", placeholder="-1", required=False, default="-1")
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            preco_float = float(self.preco.value.replace(",", "."))
            estoque_int = int(self.estoque.value)
            await db_adicionar_produto(self.pid.value, self.nome.value, preco_float, self.emoji.value, self.link.value, estoque_int)
            await interaction.followup.send(f"✅ Produto `{self.pid.value}` adicionado!", ephemeral=True)
            await atualizar_painel_loja()
            await atualizar_painel_vendas()
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

class EditarProdutoModal(discord.ui.Modal, title="Editar Produto"):
    def __init__(self, produto_id):
        super().__init__()
        self.produto_id = produto_id
        
        self.nome = discord.ui.TextInput(label="Nome", required=True)
        self.preco = discord.ui.TextInput(label="Preço", required=True)
        self.estoque = discord.ui.TextInput(label="Estoque (-1 = Ilimitado)", required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            preco_float = float(self.preco.value.replace(",", "."))
            estoque_int = int(self.estoque.value)
            await db_editar_produto(self.produto_id, self.nome.value, preco_float, estoque_int)
            await interaction.followup.send(f"✅ Produto `{self.produto_id}` editado!", ephemeral=True)
            await atualizar_painel_loja()
            await atualizar_painel_vendas()
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

async def processar_compra(interaction: discord.Interaction, key: str):
    restante = verificar_cooldown(interaction.user.id)
    if restante > 0:
        return await interaction.response.send_message(f"⏳ Aguarde **{restante}s** antes de gerar outro pagamento.", ephemeral=True)

    produtos = await db_listar_produtos()
    produto = produtos.get(key)
    if not produto:
        return await interaction.response.send_message("❌ Produto não encontrado.", ephemeral=True)

    if not await db_verificar_estoque(key):
        return await interaction.response.send_message("❌ Produto sem estoque disponível.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    
    try:
        payment_data = sdk.payment().create({
            "transaction_amount": float(produto["preco"]),
            "description": produto["nome"],
            "payment_method_id": "pix",
            "payer": {"email": f"user_{interaction.user.id}@email.com"}
        })
        
        response = payment_data["response"]
        
        if "point_of_interaction" not in response:
            return await interaction.followup.send("❌ Erro ao gerar PIX. Tente novamente.", ephemeral=True)
        
        pix_qr_code = response["point_of_interaction"]["transaction_data"]["qr_code"]
        pix_copy_paste = response["point_of_interaction"]["transaction_data"]["qr_code_base64"]
        payment_id = response["id"]
        pedido_id = str(uuid.uuid4())
        
        await db_inserir_pedido(pedido_id, interaction.user.id, str(interaction.user), key, produto["nome"], produto["preco"])
        pedidos_pendentes[payment_id] = pedido_id
        
        embed = Embed(title="💳 Pagamento PIX", description=f"**Produto:** {produto['nome']}\n**Valor:** R$ {formatar_preco(produto['preco'])}", color=Color.green())
        embed.add_field(name="📱 Código PIX (Copiar e Colar)", value=f"```\n{pix_copy_paste}\n```", inline=False)
        embed.set_image(url=pix_qr_code)
        embed.set_footer(text=f"ID: {pedido_id} | Expira em 30 minutos")
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="✅ Já paguei", style=discord.ButtonStyle.success, custom_id=f"check_{payment_id}"))
        view.add_item(discord.ui.Button(label="❌ Cancelar", style=discord.ButtonStyle.danger, custom_id=f"cancel_{payment_id}"))
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        await enviar_log("pedido", interaction.user, produto, produto["preco"], f"ID: {pedido_id}")
        registrar_cooldown(interaction.user.id)
        
        asyncio.create_task(verificar_pagamento(payment_id, pedido_id, interaction.user, produto, key))
        
    except Exception as e:
        await interaction.followup.send(f"❌ Erro ao processar pagamento: {e}", ephemeral=True)

async def verificar_pagamento(payment_id, pedido_id, user, produto, produto_key):
    for _ in range(60):
        await asyncio.sleep(30)
        try:
            payment_info = sdk.payment().get(payment_id)
            status = payment_info["response"].get("status")
            
            if status == "approved":
                if await tentar_entregar(user, produto, produto_key, pedido_id):
                    await db_marcar_entregue(pedido_id)
                    await db_decrementar_estoque(produto_key)
                    await db_incrementar_venda(float(produto["preco"]))
                    await enviar_log("venda", user, produto, produto["preco"])
                    await atualizar_painel_vendas()
                    await atualizar_painel_loja()
                else:
                    await db_marcar_falha_entrega(pedido_id)
                return
            elif status in ["cancelled", "refunded"]:
                await db_marcar_expirado(pedido_id)
                return
        except:
            pass
    
    await db_marcar_expirado(pedido_id)

async def tentar_entregar(user, produto, produto_id, pid) -> bool:
    embed = Embed(
        title="🧾 RECIBO DE COMPRA",
        description="Seu pagamento foi aprovado com sucesso!",
        color=Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="📦 Produto", value=f"**{produto['nome']}**", inline=False)
    embed.add_field(name="💰 Valor", value=f"R$ {formatar_preco(produto['preco'])}", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{produto_id}`", inline=True)
    embed.add_field(name="🔗 Entrega", value=f"[Clique aqui]({produto['link']})", inline=False)
    embed.set_footer(text="Nexzy Store • Obrigado pela compra ❤️")
    
    try:
        await user.send(embed=embed)
        return True
    except:
        return False

async def enviar_log(tipo, usuario=None, produto=None, valor=None, extra=None):
    if not WEBHOOK_LOG:
        return
    titulo = {"pedido":"🟡 NOVO PEDIDO","venda":"🟢 VENDA APROVADA","erro":"🔴 ERRO"}[tipo]
    cor = {"pedido":Color.gold(), "venda":Color.green(), "erro":Color.red()}[tipo]
    embed = Embed(title=titulo, color=cor, timestamp=datetime.now(timezone.utc))
    if usuario:
        embed.add_field(name="👤 Usuário", value=f"{usuario} ({usuario.id})", inline=False)
    if produto:
        embed.add_field(name="📦 Produto", value=produto["nome"], inline=True)
    if valor is not None:
        embed.add_field(name="💰 Valor", value=f"R$ {formatar_preco(valor)}", inline=True)
    if extra:
        embed.add_field(name="ℹ️ Info", value=str(extra), inline=False)
    try:
        async with aiohttp.ClientSession() as session:
            wh = discord.Webhook.from_url(WEBHOOK_LOG, session=session)
            await wh.send(embed=embed)
    except:
        pass

async def montar_embed_admin():
    produtos = await db_listar_produtos()
    embed = Embed(title="👑 Painel Admin", color=Color.purple())
    for pid, prod in produtos.items():
        embed.add_field(name=f"{prod.get('emoji','🛒')} {prod['nome']}", value=f"ID: `{pid}`\nPreço: R$ {formatar_preco(prod['preco'])}\nEstoque: {prod['estoque'] if prod['estoque'] != -1 else '∞'}\nVendas: {prod['vendas']}", inline=True)
    return embed

async def atualizar_painel_vendas():
    canal = bot.get_channel(CANAL_VENDAS)
    if not canal:
        print(f"⚠️ Canal de vendas {CANAL_VENDAS} não encontrado!")
        return
    embed = await montar_embed_vendas()
    msg_id = await db_get_painel_id("vendas")
    try:
        if msg_id:
            msg = await canal.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
    except:
        pass
    msg = await canal.send(embed=embed)
    await db_set_painel_id("vendas", msg.id)

async def atualizar_painel_loja():
    canal = bot.get_channel(CANAL_LOJA)
    if not canal:
        print(f"⚠️ Canal da loja {CANAL_LOJA} não encontrado!")
        return
    embed = await montar_embed_loja()
    msg_id = await db_get_painel_id("loja")
    try:
        if msg_id:
            msg = await canal.fetch_message(msg_id)
            await msg.edit(embed=embed, view=PainelPrincipal())
            return
    except:
        pass
    msg = await canal.send(embed=embed, view=PainelPrincipal())
    await db_set_painel_id("loja", msg.id)

# ================= TASKS =================
@tasks.loop(minutes=2)
async def atualizar_paineis():
    await atualizar_painel_vendas()
    await atualizar_painel_loja()

@tasks.loop(minutes=60)
async def verificar_pedidos_expirados():
    pedidos = await db_pedidos_pendentes_antigos(35)
    for pedido in pedidos:
        await db_marcar_expirado(pedido["id"])

@tasks.loop(hours=24)
async def reset_stats_diario():
    await db_reset_stats_diarias()

# ================= WEBHOOK =================
async def webhook_handler(request):
    try:
        payload = await request.read()
        signature = request.headers.get("x-signature", "")
        
        if MP_SECRET and not verificar_assinatura_mp(payload, signature, MP_SECRET):
            return web.Response(status=401, text="Assinatura inválida")
        
        data = await request.json()
        
        if data.get("type") == "payment":
            payment_id = data.get("data", {}).get("id")
            if payment_id and payment_id in pedidos_pendentes:
                payment_info = sdk.payment().get(payment_id)
                if payment_info["response"].get("status") == "approved":
                    pedido_id = pedidos_pendentes[payment_id]
                    pedido = await db_buscar_pedido(pedido_id)
                    if pedido and not pedido["entregue"]:
                        user = await bot.fetch_user(pedido["user_id"])
                        produtos = await db_listar_produtos()
                        produto = produtos.get(pedido["produto_id"])
                        if produto and await tentar_entregar(user, produto, pedido["produto_id"], pedido_id):
                            await db_marcar_entregue(pedido_id)
                            await db_decrementar_estoque(pedido["produto_id"])
                            await db_incrementar_venda(pedido["produto_preco"])
                            await atualizar_painel_vendas()
                            await atualizar_painel_loja()
                        else:
                            await db_marcar_falha_entrega(pedido_id)
        
        return web.Response(status=200, text="OK")
    except Exception as e:
        print(f"Erro no webhook: {e}")
        return web.Response(status=500, text="Erro")

async def start_webhook():
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    print(f"✅ Webhook rodando na porta {os.getenv('PORT', '8080')}")

# ================= COMANDOS =================
@bot.command(name="loja")
async def cmd_loja(ctx):
    """Envia o painel da loja no canal atual"""
    produtos = await db_listar_produtos()
    if not produtos:
        return await ctx.send("❌ Nenhum produto cadastrado ainda! Use o botão Admin para adicionar produtos.")
    
    embed = await montar_embed_loja()
    view = PainelPrincipal()
    await ctx.send(embed=embed, view=view)
    
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command(name="vendas")
async def cmd_vendas(ctx):
    """Envia o painel de vendas no canal atual"""
    embed = await montar_embed_vendas()
    await ctx.send(embed=embed)
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command(name="lojaadmin")
@commands.has_role(CARGO_DONO)
async def cmd_lojaadmin(ctx):
    """Envia o painel admin no canal atual (apenas donos)"""
    embed = await montar_embed_admin()
    await ctx.send(embed=embed, view=AdminView())
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command(name="sync")
@commands.has_role(CARGO_DONO)
async def sync_commands(ctx):
    """Sincroniza os comandos (apenas donos)"""
    await ctx.send("✅ Comandos disponíveis: `!loja`, `!vendas`, `!lojaadmin`, `!testar`, `!sync`", delete_after=10)
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command(name="testar")
async def testar_config(ctx):
    """Testa se as configurações estão corretas"""
    embed = Embed(title="🔧 Teste de Configuração", color=Color.blue())
    
    cargo = ctx.guild.get_role(CARGO_DONO)
    embed.add_field(
        name="Cargo Dono", 
        value=f"{cargo.mention if cargo else '❌ Não encontrado'}\nID: `{CARGO_DONO}`",
        inline=False
    )
    
    canal_loja = bot.get_channel(CANAL_LOJA)
    embed.add_field(
        name="Canal da Loja (Produtos)", 
        value=f"{canal_loja.mention if canal_loja else '❌ Não encontrado'}\nID: `{CANAL_LOJA}`",
        inline=False
    )
    
    canal_vendas = bot.get_channel(CANAL_VENDAS)
    embed.add_field(
        name="Canal de Vendas (Estatísticas)", 
        value=f"{canal_vendas.mention if canal_vendas else '❌ Não encontrado'}\nID: `{CANAL_VENDAS}`",
        inline=False
    )
    
    canal_falhas = bot.get_channel(CANAL_FALHAS)
    embed.add_field(
        name="Canal Falhas", 
        value=f"{canal_falhas.mention if canal_falhas else '❌ Não encontrado'}\nID: `{CANAL_FALHAS}`",
        inline=False
    )
    
    tem_cargo = any(r.id == CARGO_DONO for r in ctx.author.roles)
    embed.add_field(
        name="Seu Status", 
        value="✅ Você é dono" if tem_cargo else "❌ Você NÃO é dono",
        inline=False
    )
    
    produtos = await db_listar_produtos()
    embed.add_field(
        name="📦 Produtos Cadastrados",
        value=str(len(produtos)) if produtos else "0 - Use o botão Admin para adicionar",
        inline=False
    )
    
    await ctx.send(embed=embed)

# ================= EVENTOS =================
@bot.event
async def on_ready():
    print(f"✅ Bot logado como {bot.user}")
    print(f"🛒 Canal da Loja (Produtos): {CANAL_LOJA}")
    print(f"📊 Canal de Vendas (Estatísticas): {CANAL_VENDAS}")
    print(f"👑 Cargo Dono: {CARGO_DONO}")
    print(f"⚠️ Canal Falhas: {CANAL_FALHAS}")
    
    await init_db()
    
    if db_pool is None:
        print("❌ Banco de dados não conectado!")
        return
    
    await atualizar_painel_loja()
    await atualizar_painel_vendas()
    
    atualizar_paineis.start()
    verificar_pedidos_expirados.start()
    reset_stats_diario.start()
    
    asyncio.create_task(start_webhook())
    
    print(f"✅ Bot pronto!")
    print(f"📌 Use !loja para enviar a loja em qualquer canal")
    print(f"📌 Use !vendas para ver as estatísticas")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")
        
        if custom_id.startswith("check_"):
            payment_id = int(custom_id.split("_")[1])
            await interaction.response.send_message("⏳ Verificando pagamento...", ephemeral=True)
            try:
                payment_info = sdk.payment().get(payment_id)
                if payment_info["response"].get("status") == "approved":
                    await interaction.edit_original_response(content="✅ Pagamento já foi aprovado! Verifique sua DM.", embed=None, view=None)
                else:
                    await interaction.edit_original_response(content="⏳ Pagamento ainda não identificado. Aguarde alguns minutos.", embed=None, view=None)
            except:
                await interaction.edit_original_response(content="❌ Erro ao verificar pagamento. Tente novamente mais tarde.", embed=None, view=None)
        
        elif custom_id.startswith("cancel_"):
            await interaction.response.send_message("❌ Pedido cancelado.", ephemeral=True)

# ================= MAIN =================
async def start_bot():
    """Inicia o bot com tratamento de rate limit"""
    try:
        await bot.start(DISCORD_TOKEN)
    except discord.errors.HTTPException as e:
        if e.status == 429:
            print("❌ Rate limit do Discord. Aguardando 30 segundos...")
            await asyncio.sleep(30)
            await start_bot()  # Tenta novamente
        else:
            raise e
    except Exception as e:
        print(f"❌ Erro ao iniciar: {e}")
        raise e

if __name__ == "__main__":
    # Desabilitar reconexão automática muito rápida
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(start_bot())
    except KeyboardInterrupt:
        print("🛑 Bot desligado manualmente")
    finally:
        loop.close()