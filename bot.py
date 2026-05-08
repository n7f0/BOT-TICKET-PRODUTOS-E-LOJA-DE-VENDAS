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
CANAL_STATS   = int(os.getenv("CANAL_STATS", "0"))
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
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                versao INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS produtos (
                id          TEXT PRIMARY KEY,
                nome        TEXT NOT NULL,
                preco       NUMERIC(10,2) NOT NULL,
                emoji       TEXT DEFAULT '🛒',
                link        TEXT NOT NULL,
                estoque     INTEGER DEFAULT -1,
                vendas      INTEGER DEFAULT 0,
                criado_em   TIMESTAMPTZ DEFAULT NOW()
            );

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
            );

            CREATE TABLE IF NOT EXISTS estatisticas (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS painel_ids (
                nome   TEXT PRIMARY KEY,
                msg_id BIGINT NOT NULL
            );

            INSERT INTO estatisticas (chave, valor)
            VALUES ('vendas','0'),('faturamento','0.0'),('vendas_hoje','0'),('faturamento_hoje','0.0'),('ultima_reset','')
            ON CONFLICT (chave) DO NOTHING;
        """)
        
        row = await conn.fetchrow("SELECT versao FROM schema_version LIMIT 1")
        versao_atual = row["versao"] if row else 0

        if versao_atual < 1:
            await conn.execute("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS estoque INTEGER DEFAULT -1")
            await conn.execute("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS vendas INTEGER DEFAULT 0")
        if versao_atual < 2:
            await conn.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tentativas INTEGER DEFAULT 0")
        if versao_atual < 3:
            await conn.execute("""
                INSERT INTO estatisticas (chave, valor)
                VALUES ('vendas_hoje','0'),('faturamento_hoje','0.0'),('ultima_reset','')
                ON CONFLICT (chave) DO NOTHING
            """)

        await conn.execute("""
            INSERT INTO schema_version (versao) VALUES ($1)
            ON CONFLICT (versao) DO UPDATE SET versao=$1
        """, SCHEMA_VERSION)

    print(f"✅ Banco de dados inicializado (schema v{SCHEMA_VERSION}).")

async def db_listar_produtos() -> dict:
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
        await conn.execute("""
            UPDATE estatisticas SET valor=(valor::NUMERIC+1)::TEXT   WHERE chave='vendas';
            UPDATE estatisticas SET valor=(valor::NUMERIC+$1)::TEXT  WHERE chave='faturamento';
            UPDATE estatisticas SET valor=(valor::NUMERIC+1)::TEXT   WHERE chave='vendas_hoje';
            UPDATE estatisticas SET valor=(valor::NUMERIC+$1)::TEXT  WHERE chave='faturamento_hoje';
        """, preco)

async def db_reset_stats_diarias():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE estatisticas SET valor='0'   WHERE chave='vendas_hoje';
            UPDATE estatisticas SET valor='0.0' WHERE chave='faturamento_hoje';
            UPDATE estatisticas SET valor=$1    WHERE chave='ultima_reset';
        """, datetime.now(timezone.utc).isoformat())

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
async def montar_embed_privado():
    vendas           = await db_get_stat("vendas")
    faturamento      = await db_get_stat("faturamento")
    vendas_hoje      = await db_get_stat("vendas_hoje")
    fat_hoje         = await db_get_stat("faturamento_hoje")
    mais_vendido     = await db_produto_mais_vendido()
    falhas           = await db_pedidos_falha_pendentes()

    embed = Embed(title="📊 PAINEL PRIVADO", color=Color.dark_gold(),
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
    embed = Embed(title="🛒 NEXZY STORE",
                  description="💎 Compre automaticamente via PIX",
                  color=Color.dark_blue())
    embed.set_image(url="https://media.discordapp.net/attachments/1491808878562643998/1491808965170958396/e6876514-c5ae-477f-a84b-d7b7db0c01e5.png")

    for k, p in produtos.items():
        estoque_txt = "∞ Ilimitado" if p["estoque"] == -1 else (
            f"✅ {p['estoque']} em estoque" if p["estoque"] > 0 else "❌ Esgotado"
        )
        embed.add_field(
            name=f"{p.get('emoji','🛒')} {p['nome']}",
            value=f"💰 R$ {formatar_preco(p['preco'])}\n📦 {estoque_txt}\n🆔 ID: `{k}`",
            inline=True
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
        self.add_item(discord.ui.Button(label="💰 Comprar", style=discord.ButtonStyle.success, custom_id="btn_comprar"))
        self.add_item(discord.ui.Button(label="📜 Meus Pedidos", style=discord.ButtonStyle.secondary, custom_id="btn_pedidos"))
    
    @discord.ui.button(label="🎁 Resgatar", style=discord.ButtonStyle.primary, custom_id="btn_resgatar", row=1)
    async def btn_resgatar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResgatarModal())
    
    @discord.ui.button(label="👑 Admin", style=discord.ButtonStyle.danger, custom_id="btn_admin", row=1)
    async def btn_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not eh_dono(interaction):
            return await interaction.response.send_message("❌ Apenas administradores podem acessar.", ephemeral=True)
        await interaction.response.send_message(embed=await montar_embed_admin(), view=AdminView(), ephemeral=True)

class ResgatarModal(discord.ui.Modal, title="🎁 Resgatar Produto"):
    codigo = discord.ui.TextInput(label="Código de Resgate", placeholder="Cole seu código aqui...", style=discord.TextStyle.paragraph)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ Verificando código...", ephemeral=True)
        codigo = self.codigo.value.strip()
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM pedidos WHERE id=$1 AND status='aprovado' AND entregue=false", codigo)
            if not row:
                return await interaction.edit_original_response(content="❌ Código inválido ou já resgatado!", embed=None, view=None)
            produto = await db_listar_produtos()
            prod = produto.get(row["produto_id"])
            if prod:
                embed = Embed(title="🎁 Produto Resgatado!", color=Color.green())
                embed.add_field(name="📦 Produto", value=row["produto_nome"], inline=False)
                embed.add_field(name="🔗 Link", value=f"[Clique aqui]({prod['link']})", inline=False)
                await interaction.user.send(embed=embed)
                await conn.execute("UPDATE pedidos SET entregue=true WHERE id=$1", codigo)
                await interaction.edit_original_response(content="✅ Produto resgatado! Verifique sua DM.", embed=None, view=None)
            else:
                await interaction.edit_original_response(content="❌ Produto não encontrado!", embed=None, view=None)

class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    
    @discord.ui.button(label="➕ Adicionar Produto", style=discord.ButtonStyle.success)
    async def add_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdicionarProdutoModal())
    
    @discord.ui.button(label="✏️ Editar Produto", style=discord.ButtonStyle.primary)
    async def edit_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db_listar_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        view = SelecionarProdutoEditar(produtos)
        await interaction.response.send_message("Selecione o produto para editar:", view=view, ephemeral=True)
    
    @discord.ui.button(label="🗑️ Remover Produto", style=discord.ButtonStyle.danger)
    async def remove_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db_listar_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        view = SelecionarProdutoRemover(produtos)
        await interaction.response.send_message("Selecione o produto para remover:", view=view, ephemeral=True)
    
    @discord.ui.button(label="📊 Estatísticas", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await montar_embed_privado()
        await interaction.response.send_message(embed=embed, ephemeral=True)

class SelecionarProdutoEditar(discord.ui.View):
    def __init__(self, produtos: dict):
        super().__init__(timeout=60)
        select = discord.ui.Select(placeholder="Selecione um produto...")
        for pid, prod in produtos.items():
            select.add_option(label=f"{prod['nome']} - R$ {prod['preco']}", value=pid, emoji=prod.get('emoji', '🛒'))
        select.callback = self.select_callback
        self.add_item(select)
    
    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditarProdutoModal(self.select.values[0]))

class SelecionarProdutoRemover(discord.ui.View):
    def __init__(self, produtos: dict):
        super().__init__(timeout=60)
        select = discord.ui.Select(placeholder="Selecione um produto...")
        for pid, prod in produtos.items():
            select.add_option(label=f"{prod['nome']} - R$ {prod['preco']}", value=pid, emoji=prod.get('emoji', '🛒'))
        select.callback = self.select_callback
        self.add_item(select)
    
    async def select_callback(self, interaction: discord.Interaction):
        await db_remover_produto(self.select.values[0])
        await interaction.response.send_message("✅ Produto removido com sucesso!", ephemeral=True)

class AdicionarProdutoModal(discord.ui.Modal, title="Adicionar Produto"):
    pid = discord.ui.TextInput(label="ID do Produto", placeholder="ex: produto1", required=True)
    nome = discord.ui.TextInput(label="Nome", placeholder="Nome do produto", required=True)
    preco = discord.ui.TextInput(label="Preço", placeholder="19.90", required=True)
    emoji = discord.ui.TextInput(label="Emoji", placeholder="🛒", required=False, default="🛒")
    link = discord.ui.TextInput(label="Link de Entrega", placeholder="https://...", required=True)
    estoque = discord.ui.TextInput(label="Estoque (-1 = Ilimitado)", placeholder="-1", required=False, default="-1")
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_float = float(self.preco.value.replace(",", "."))
            estoque_int = int(self.estoque.value)
            await db_adicionar_produto(self.pid.value, self.nome.value, preco_float, self.emoji.value, self.link.value, estoque_int)
            await interaction.response.send_message(f"✅ Produto `{self.pid.value}` adicionado!", ephemeral=True)
            await atualizar_painel_loja()
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro: {e}", ephemeral=True)

class EditarProdutoModal(discord.ui.Modal, title="Editar Produto"):
    def __init__(self, produto_id):
        super().__init__()
        self.produto_id = produto_id
    
    nome = discord.ui.TextInput(label="Nome", required=True)
    preco = discord.ui.TextInput(label="Preço", required=True)
    estoque = discord.ui.TextInput(label="Estoque (-1 = Ilimitado)", required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_float = float(self.preco.value.replace(",", "."))
            estoque_int = int(self.estoque.value)
            await db_editar_produto(self.produto_id, self.nome.value, preco_float, estoque_int)
            await interaction.response.send_message(f"✅ Produto `{self.produto_id}` editado!", ephemeral=True)
            await atualizar_painel_loja()
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro: {e}", ephemeral=True)

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
                    await atualizar_painel_privado()
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

async def atualizar_painel_privado():
    canal = bot.get_channel(CANAL_STATS)
    if not canal:
        return
    embed = await montar_embed_privado()
    msg_id = await db_get_painel_id("privado")
    try:
        if msg_id:
            msg = await canal.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
    except:
        pass
    msg = await canal.send(embed=embed)
    await db_set_painel_id("privado", msg.id)

async def atualizar_painel_loja():
    canal = bot.get_channel(CANAL_STATS)
    if not canal:
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
    await atualizar_painel_privado()
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
                            await atualizar_painel_privado()
                            await atualizar_painel_loja()
                        else:
                            await db_marcar_falha_entrega(pedido_id)
        
        return web.Response(status=200, text="OK")
    except:
        return web.Response(status=500, text="Erro")

async def start_webhook():
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    print(f"✅ Webhook rodando na porta {os.getenv('PORT', '8080')}")

# ================= EVENTOS =================
@bot.event
async def on_ready():
    print(f"✅ Bot logado como {bot.user}")
    await init_db()
    await atualizar_painel_loja()
    await atualizar_painel_privado()
    atualizar_paineis.start()
    verificar_pedidos_expirados.start()
    reset_stats_diario.start()
    asyncio.create_task(start_webhook())

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")
        
        if custom_id.startswith("check_"):
            payment_id = int(custom_id.split("_")[1])
            await interaction.response.send_message("⏳ Verificando pagamento...", ephemeral=True)
            payment_info = sdk.payment().get(payment_id)
            if payment_info["response"].get("status") == "approved":
                await interaction.edit_original_response(content="✅ Pagamento já foi aprovado! Verifique sua DM.", embed=None, view=None)
            else:
                await interaction.edit_original_response(content="⏳ Pagamento ainda não identificado. Aguarde alguns minutos.", embed=None, view=None)
        
        elif custom_id.startswith("cancel_"):
            await interaction.response.send_message("❌ Pedido cancelado.", ephemeral=True)
            await interaction.edit_original_response(content="❌ Compra cancelada.", embed=None, view=None)
        
        elif custom_id == "btn_comprar":
            produtos = await db_listar_produtos()
            disponiveis = {k: v for k, v in produtos.items() if v["estoque"] != 0}
            if not disponiveis:
                return await interaction.response.send_message("❌ Nenhum produto disponível no momento.", ephemeral=True)
            view = discord.ui.View()
            view.add_item(SelecionarProduto(disponiveis))
            await interaction.response.send_message("Selecione o produto desejado:", view=view, ephemeral=True)
        
        elif custom_id == "btn_pedidos":
            pedidos = await db_pedidos_usuario(interaction.user.id)
            if not pedidos:
                return await interaction.response.send_message("📭 Você não tem nenhum pedido.", ephemeral=True)
            embed = Embed(title="📜 Seus Pedidos", color=Color.blue())
            for p in pedidos[:10]:
                embed.add_field(name=f"{status_emoji(p['status'])} {p['produto_nome']}", value=f"ID: `{p['id']}`\nValor: R$ {formatar_preco(p['produto_preco'])}\nData: {p['criado_em'].strftime('%d/%m/%Y %H:%M')}", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= MAIN =================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)