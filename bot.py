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
MP_SECRET     = os.getenv("MP_WEBHOOK_SECRET", "")  # Chave secreta do webhook MP
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

# Cooldown por usuário: user_id -> datetime do último pedido
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
        # Migração de schema
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


# ── Produtos ──────────────────────────────────────────────
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
    """Retorna True se há estoque disponível (ou se é ilimitado: -1)."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT estoque FROM produtos WHERE id=$1", pid)
    if not row:
        return False
    return row["estoque"] == -1 or row["estoque"] > 0


# ── Pedidos ───────────────────────────────────────────────
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
    """Busca pedidos pendentes mais antigos que X minutos (PIX expirado)."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM pedidos
            WHERE status='pendente'
            AND criado_em < NOW() - ($1 * INTERVAL '1 minute')
        """, minutos)
    return [dict(r) for r in rows]


# ── Estatísticas ──────────────────────────────────────────
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


# ── IDs de mensagens do painel ────────────────────────────
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


# ================= HELPERS =================
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
    """Valida a assinatura HMAC-SHA256 enviada pelo Mercado Pago."""
    if not secret or not header_signature:
        return True  # Se não configurou o secret, aceita tudo (modo dev)
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
    """Retorna 0 se liberado, ou os segundos restantes."""
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


# ================= UTIL =================
async def atualizar_painel_privado():
    canal = bot.get_channel(CANAL_STATS)
    if not canal:
        return
    embed  = await montar_embed_privado()
    msg_id = await db_get_painel_id("privado")
    try:
        if msg_id:
            msg = await canal.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
    except Exception:
        pass
    msg = await canal.send(embed=embed)
    await db_set_painel_id("privado", msg.id)

async def atualizar_painel_loja():
    canal = bot.get_channel(CANAL_STATS)
    if not canal:
        return
    embed  = await montar_embed_loja()
    msg_id = await db_get_painel_id("loja")
    try:
        if msg_id:
            msg = await canal.fetch_message(msg_id)
            await msg.edit(embed=embed, view=PainelPrincipal())
            return
    except Exception:
        pass
    msg = await canal.send(embed=embed, view=PainelPrincipal())
    await db_set_painel_id("loja", msg.id)

async def notificar_falha(user, titulo, descricao):
    canal = bot.get_channel(CANAL_FALHAS)
    if not canal:
        return
    embed = Embed(title=titulo, description=descricao, color=Color.red(),
                  timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Usuário", value=f"{user} ({user.id})", inline=False)
    try:
        await canal.send(embed=embed)
    except Exception:
        pass

async def tentar_entregar(user, produto, produto_id, pid) -> bool:
    """Envia DM de entrega. Retorna True se entregue com sucesso."""
    embed = Embed(
        title="🧾 RECIBO DE COMPRA",
        description="Seu pagamento foi aprovado com sucesso!",
        color=Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="📦 Produto", value=f"**{produto['nome']}**",                inline=False)
    embed.add_field(name="💰 Valor",   value=f"R$ {formatar_preco(produto['preco'])}", inline=True)
    embed.add_field(name="🆔 ID",      value=f"`{produto_id}`",                        inline=True)
    embed.add_field(name="🔗 Entrega", value=f"[Clique aqui]({produto['link']})",      inline=False)
    embed.set_footer(text="Nexzy Store • Obrigado pela compra ❤️")
    try:
        await user.send(embed=embed)
        return True
    except discord.Forbidden:
        await enviar_log("erro", user, produto, produto["preco"], extra="DM fechada/bloqueada")
        await notificar_falha(user, "❌ Falha na entrega", "Usuário com DM fechada ou bloqueada.")
        return False
    except Exception as e:
        await enviar_log("erro", user, produto, produto["preco"], extra=str(e))
        await notificar_falha(user, "❌ Falha na entrega", f"Erro: {e}")
        return False


# ================= LOG =================
class LogView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(discord.ui.Button(label="👤 Ver Perfil",
                                        url=f"https://discord.com/users/{user_id}"))

async def enviar_log(tipo, usuario=None, produto=None, valor=None, extra=None):
    if not WEBHOOK_LOG:
        return
    titulo = {"pedido":"🟡 NOVO PEDIDO","venda":"🟢 VENDA APROVADA","erro":"🔴 ERRO"}[tipo]
    cor    = {"pedido":Color.gold(),    "venda":Color.green(),       "erro":Color.red()}[tipo]
    embed  = Embed(title=titulo, color=cor, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Nexzy Store • Sistema Automático")
    if usuario:
        embed.add_field(name="👤 Usuário", value=f"{usuario} ({usuario.id})", inline=False)
    if produto:
        embed.add_field(name="📦 Produto", value=produto["nome"],                  inline=True)
    if valor is not None:
        embed.add_field(name="💰 Valor",   value=f"R$ {formatar_preco(valor)}",    inline=True)
    if extra:
        embed.add_field(name="ℹ️ Info",    value=str(extra),                       inline=False)
    try:
        async with aiohttp.ClientSession() as session:
            wh = discord.Webhook.from_url(WEBHOOK_LOG, session=session)
            await wh.send(embed=embed, view=LogView(usuario.id) if usuario else None)
    except Exception as e:
        print(f"[ERRO LOG] {e}")


# ================= PAGAMENTO =================
def criar_pagamento(user_id, produto):
    return sdk.payment().create({
        "transaction_amount": float(produto["preco"]),
        "description":        produto["nome"],
        "payment_method_id":  "pix",
        "payer":              {"email": f"user_{user_id}@email.com"}
    })["response"]

async def processar_compra(interaction: discord.Interaction, key: str):
    # Cooldown
    restante = verificar_cooldown(interaction.user.id)
    if restante > 0:
        return await interaction.response.send_message(
            f"⏳ Aguarde **{restante}s** antes de gerar outro pagamento.", ephemeral=True)

    produtos = await db_listar_produtos()
    produto  = produtos.get(key)
    if not produto:
        return await interaction.response.send_message("❌ Produto não encontrado.", ephemeral=True)

    # Estoque
    if not await db_verificar_estoque(key):
        return await interaction.response.send_message("❌ Produto sem estoque disponível.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    try:
        loop      = asyncio.get_running_loop()
        pagamento = await loop.run_in_executor(None, criar_pagamento, interaction.user.id, produto)
    except Exception as e:
        return await interaction.followup.send(f"❌ Erro ao gerar pagamento: `{e}`", ephemeral=True)

    try:
        pix = pagamento["point_of_interaction"]["transaction_data"]["qr_code"]
    except Exception:
        return await interaction.followup.send("❌ Erro ao extrair código PIX.", ephemeral=True)

    pid  = str(pagamento["id"])
    user = interaction.user

    await db_inserir_pedido(pid, user.id, str(user), key, produto["nome"], float(produto["preco"]))
    pedidos_pendentes[pid] = {"user_id": user.id, "produto": produto, "produto_id": key}
    registrar_cooldown(user.id)

    await enviar_log("pedido", user, produto, produto["preco"])

    embed = Embed(title="💳 Pagamento PIX", color=Color.green())
    embed.add_field(name="Produto", value=produto["nome"],                          inline=True)
    embed.add_field(name="Valor",   value=f"R$ {formatar_preco(produto['preco'])}", inline=True)
    embed.add_field(name="🔑 Copia e Cola", value=f"```{pix}```",                  inline=False)
    embed.set_footer(text="⏱️ PIX válido por 30 minutos. Você receberá o produto via DM.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ================= MODAIS =================
class AddModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Adicionar Produto")
        self.nome    = discord.ui.TextInput(label="Nome")
        self.preco   = discord.ui.TextInput(label="Preço (ex: 10.00)")
        self.emoji   = discord.ui.TextInput(label="Emoji", required=False)
        self.link    = discord.ui.TextInput(label="Link de entrega")
        self.estoque = discord.ui.TextInput(label="Estoque (-1 = ilimitado)", default="-1")
        for item in [self.nome, self.preco, self.emoji, self.link, self.estoque]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_val   = float(self.preco.value.replace(",", "."))
            estoque_val = int(self.estoque.value)
        except ValueError:
            return await interaction.response.send_message("❌ Preço ou estoque inválido.", ephemeral=True)
        pid = f"produto_{uuid.uuid4().hex[:6]}"
        await db_adicionar_produto(pid, self.nome.value, preco_val,
                                   self.emoji.value or "🛒", self.link.value, estoque_val)
        await atualizar_painel_loja()
        await interaction.response.send_message(
            f"✅ **{self.nome.value}** adicionado!\nID: `{pid}`", ephemeral=True)

class EditModal(discord.ui.Modal):
    def __init__(self, key: str, produto: dict):
        super().__init__(title="Editar Produto")
        self.key     = key
        self.nome    = discord.ui.TextInput(label="Novo nome",    default=produto["nome"],                  required=True)
        self.preco   = discord.ui.TextInput(label="Novo valor",   default=formatar_preco(produto["preco"]), required=True)
        self.estoque = discord.ui.TextInput(label="Estoque (-1=ilimitado)", default=str(produto["estoque"]),required=True)
        for item in [self.nome, self.preco, self.estoque]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_val   = float(self.preco.value.replace(",", "."))
            estoque_val = int(self.estoque.value)
        except ValueError:
            return await interaction.response.send_message("❌ Valor ou estoque inválido.", ephemeral=True)
        await db_editar_produto(self.key, self.nome.value, preco_val, estoque_val)
        await atualizar_painel_loja()
        await interaction.response.send_message(f"✅ Produto `{self.key}` atualizado!", ephemeral=True)

class ReentregaModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Reentrega Manual")
        self.pid = discord.ui.TextInput(label="ID do Pagamento (Mercado Pago)")
        self.add_item(self.pid)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        pid    = self.pid.value.strip()
        pedido = await db_buscar_pedido(pid)

        if not pedido:
            return await interaction.followup.send("❌ Pedido não encontrado.", ephemeral=True)
        if pedido["entregue"]:
            return await interaction.followup.send("⚠️ Este pedido já foi entregue.", ephemeral=True)

        produtos = await db_listar_produtos()
        produto  = produtos.get(pedido["produto_id"], {
            "nome": pedido["produto_nome"], "preco": pedido["produto_preco"], "link": "#"
        })

        try:
            user = await bot.fetch_user(pedido["user_id"])
        except Exception:
            return await interaction.followup.send("❌ Não foi possível encontrar o usuário.", ephemeral=True)

        entregue = await tentar_entregar(user, produto, pedido["produto_id"], pid)
        if entregue:
            await db_marcar_entregue(pid)
            await enviar_log("venda", user, produto, produto["preco"], extra="Reentrega manual")
            await interaction.followup.send(f"✅ Entrega realizada com sucesso para **{user}**!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Falha ao enviar DM. DM do usuário pode estar fechada.", ephemeral=True)


# ================= SELECTS =================
class SelectProdutos(discord.ui.Select):
    def __init__(self, produtos: dict):
        opts = [
            discord.SelectOption(
                label=p["nome"][:100],
                description=f"R$ {formatar_preco(p['preco'])} | {'Esgotado' if p['estoque']==0 else ('∞' if p['estoque']==-1 else str(p['estoque'])+' un.')}",
                emoji=p.get("emoji", "🛒"),
                value=k
            ) for k, p in list(produtos.items())[:25]
        ]
        super().__init__(placeholder="Selecione um produto...", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        await processar_compra(interaction, self.values[0])

class SelectEditarProdutos(discord.ui.Select):
    def __init__(self, produtos: dict):
        self._produtos = produtos
        opts = [
            discord.SelectOption(label=p["nome"][:100],
                                 description=f"Editar · R$ {formatar_preco(p['preco'])}",
                                 emoji=p.get("emoji","🛒"), value=k)
            for k, p in list(produtos.items())[:25]
        ]
        super().__init__(placeholder="Escolha o produto para editar...", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        produto = self._produtos.get(self.values[0])
        await interaction.response.send_modal(EditModal(self.values[0], produto))

class SelectRemoverProdutos(discord.ui.Select):
    def __init__(self, produtos: dict):
        opts = [
            discord.SelectOption(label=p["nome"][:100],
                                 description=f"Remover · R$ {formatar_preco(p['preco'])}",
                                 emoji=p.get("emoji","🛒"), value=k)
            for k, p in list(produtos.items())[:25]
        ]
        super().__init__(placeholder="Escolha o produto para remover...", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        await db_remover_produto(self.values[0])
        await atualizar_painel_loja()
        await interaction.response.send_message("✅ Produto removido.", ephemeral=True)


# ================= VIEWS =================
class BotaoVoltarPrincipal(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔙 Voltar", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        embed = await montar_embed_loja()
        await interaction.response.send_message(embed=embed, view=PainelPrincipal(), ephemeral=True)

class BotaoVoltarAdmin(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔙 Voltar", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Menu administrativo:", view=ViewAdmin(), ephemeral=True)

class ViewProdutos(discord.ui.View):
    def __init__(self, produtos: dict):
        super().__init__(timeout=300)
        self.add_item(SelectProdutos(produtos))
        self.add_item(BotaoVoltarPrincipal())

class ViewEditarProdutos(discord.ui.View):
    def __init__(self, produtos: dict):
        super().__init__(timeout=300)
        self.add_item(SelectEditarProdutos(produtos))
        self.add_item(BotaoVoltarAdmin())

class ViewRemoverProdutos(discord.ui.View):
    def __init__(self, produtos: dict):
        super().__init__(timeout=300)
        self.add_item(SelectRemoverProdutos(produtos))
        self.add_item(BotaoVoltarAdmin())

class BotaoAbrirProdutos(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📦 Produtos", style=discord.ButtonStyle.success, custom_id="btn_produtos")

    async def callback(self, interaction: discord.Interaction):
        produtos = await db_listar_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        await interaction.response.send_message("Escolha um produto:", view=ViewProdutos(produtos), ephemeral=True)

class BotaoAdmin(discord.ui.Button):
    def __init__(self):
        super().__init__(label="⚙️ Admin", style=discord.ButtonStyle.secondary, custom_id="btn_admin")

    async def callback(self, interaction: discord.Interaction):
        if not eh_dono(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_message("Menu administrativo:", view=ViewAdmin(), ephemeral=True)

class BotaoAdd(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Adicionar", style=discord.ButtonStyle.primary, custom_id="admin_add")

    async def callback(self, interaction: discord.Interaction):
        if not eh_dono(interaction): return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_modal(AddModal())

class BotaoEditar(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✏️ Editar", style=discord.ButtonStyle.secondary, custom_id="admin_edit")

    async def callback(self, interaction: discord.Interaction):
        if not eh_dono(interaction): return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        produtos = await db_listar_produtos()
        await interaction.response.send_message("Selecione o produto para editar:", view=ViewEditarProdutos(produtos), ephemeral=True)

class BotaoRemover(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🗑️ Remover", style=discord.ButtonStyle.danger, custom_id="admin_remove")

    async def callback(self, interaction: discord.Interaction):
        if not eh_dono(interaction): return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        produtos = await db_listar_produtos()
        await interaction.response.send_message("Selecione o produto para remover:", view=ViewRemoverProdutos(produtos), ephemeral=True)

class BotaoReentregar(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔁 Reentrega", style=discord.ButtonStyle.primary, custom_id="admin_reentrega")

    async def callback(self, interaction: discord.Interaction):
        if not eh_dono(interaction): return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_modal(ReentregaModal())

class ViewAdmin(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(BotaoAdd())
        self.add_item(BotaoEditar())
        self.add_item(BotaoRemover())
        self.add_item(BotaoReentregar())
        self.add_item(BotaoVoltarPrincipal())

class PainelPrincipal(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BotaoAbrirProdutos())
        self.add_item(BotaoAdmin())


# ================= WEBHOOK MERCADO PAGO =================
async def mp_webhook(request):
    raw = await request.read()

    # Validação de assinatura
    sig = request.headers.get("x-signature", "")
    if MP_SECRET and not verificar_assinatura_mp(raw, sig, MP_SECRET):
        print("[WEBHOOK] Assinatura inválida — requisição rejeitada.")
        return web.Response(status=401)

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)

    if data.get("type") == "payment":
        pid = str(data["data"]["id"])

        pedido_db = await db_buscar_pedido(pid)
        if pedido_db and pedido_db["entregue"]:
            return web.Response(status=200)

        try:
            info   = sdk.payment().get(pid)
            status = info["response"].get("status")
        except Exception as e:
            print(f"[ERRO verificar pagamento] {e}")
            return web.Response(status=200)

        if status == "approved":
            await _processar_aprovacao(pid, pedido_db)

    return web.Response(status=200)

async def _processar_aprovacao(pid: str, pedido_db: dict | None):
    pedido = pedidos_pendentes.pop(pid, None)

    if not pedido and pedido_db:
        produtos = await db_listar_produtos()
        pedido = {
            "user_id":    pedido_db["user_id"],
            "produto_id": pedido_db["produto_id"],
            "produto":    produtos.get(pedido_db["produto_id"], {
                "nome": pedido_db["produto_nome"],
                "preco": pedido_db["produto_preco"],
                "link": "#"
            })
        }

    if not pedido:
        return

    try:
        user    = await bot.fetch_user(pedido["user_id"])
        produto = pedido["produto"]

        entregue = await tentar_entregar(user, produto, pedido["produto_id"], pid)

        await db_incrementar_venda(float(produto["preco"]))
        await db_decrementar_estoque(pedido["produto_id"])

        if entregue:
            await db_marcar_entregue(pid)
            await enviar_log("venda", user, produto, produto["preco"])
        else:
            await db_marcar_falha_entrega(pid)

        await atualizar_painel_privado()
    except Exception as e:
        print(f"[ERRO entrega] {e}")

async def start_web():
    app = web.Application()
    app.router.add_post("/mp", mp_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    porta = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", porta).start()
    print(f"✅ Webhook MP rodando na porta {porta}")


# ================= TASKS =================
@tasks.loop(minutes=2)
async def atualizar_sistema():
    await atualizar_painel_privado()

@tasks.loop(minutes=10)
async def verificar_pagamentos_pendentes():
    """Verifica no MP pagamentos que o webhook pode ter perdido."""
    for pid, pedido in list(pedidos_pendentes.items()):
        try:
            info   = sdk.payment().get(pid)
            status = info["response"].get("status")
            if status == "approved":
                pedido_db = await db_buscar_pedido(pid)
                if not (pedido_db and pedido_db["entregue"]):
                    await _processar_aprovacao(pid, pedido_db)
                    print(f"[BACKUP] Pagamento {pid} processado via verificação ativa.")
        except Exception as e:
            print(f"[BACKUP ERRO] {pid}: {e}")

@tasks.loop(minutes=15)
async def expirar_pedidos_antigos():
    """Marca como expirado pedidos PIX que passaram de 35 minutos sem pagamento."""
    expirados = await db_pedidos_pendentes_antigos(35)
    for p in expirados:
        await db_marcar_expirado(p["id"])
        pedidos_pendentes.pop(p["id"], None)
    if expirados:
        print(f"[EXPIRAÇÃO] {len(expirados)} pedido(s) marcados como expirados.")

@tasks.loop(hours=1)
async def reset_stats_diarias():
    """Zera as estatísticas de hoje à meia-noite."""
    ultima = await db_get_stat_str("ultima_reset")
    agora  = datetime.now(timezone.utc)
    hoje   = agora.strftime("%Y-%m-%d")

    if ultima and ultima[:10] == hoje:
        return
    if agora.hour == 0:
        await db_reset_stats_diarias()
        print("[RESET] Estatísticas diárias zeradas.")


# ================= COMANDOS =================
@bot.command()
async def loja(ctx):
    embed = await montar_embed_loja()
    await ctx.send(embed=embed, view=PainelPrincipal())

@bot.command(name="minhas_compras")
async def minhas_compras(ctx):
    """Usuário vê suas últimas 10 compras."""
    pedidos = await db_pedidos_usuario(ctx.author.id)
    if not pedidos:
        return await ctx.author.send("📭 Você ainda não realizou nenhuma compra.")

    embed = Embed(title="🧾 Suas Últimas Compras", color=Color.blurple(),
                  timestamp=datetime.now(timezone.utc))
    for p in pedidos:
        embed.add_field(
            name=f"{status_emoji(p['status'])} {p['produto_nome']}",
            value=f"💰 R$ {formatar_preco(p['produto_preco'])} · {p['criado_em'].strftime('%d/%m/%Y %H:%M')}",
            inline=False
        )
    try:
        await ctx.author.send(embed=embed)
        if ctx.guild:
            await ctx.message.add_reaction("📬")
    except discord.Forbidden:
        await ctx.send("❌ Não consegui te enviar DM. Abra suas DMs e tente novamente.", delete_after=10)

@bot.command(name="pedido")
@commands.check(lambda ctx: any(r.id == CARGO_DONO for r in ctx.author.roles))
async def ver_pedido(ctx, pid: str):
    """Admin: busca um pedido pelo ID."""
    pedido = await db_buscar_pedido(pid)
    if not pedido:
        return await ctx.send("❌ Pedido não encontrado.", delete_after=10)

    embed = Embed(title=f"🔍 Pedido `{pid}`", color=Color.blurple(),
                  timestamp=datetime.now(timezone.utc))
    embed.add_field(name="👤 Usuário",  value=f"{pedido['user_tag']} ({pedido['user_id']})", inline=False)
    embed.add_field(name="📦 Produto",  value=pedido["produto_nome"],                         inline=True)
    embed.add_field(name="💰 Valor",    value=f"R$ {formatar_preco(pedido['produto_preco'])}", inline=True)
    embed.add_field(name="📊 Status",   value=f"{status_emoji(pedido['status'])} {pedido['status']}", inline=True)
    embed.add_field(name="🔁 Tentativas", value=str(pedido["tentativas"]),                    inline=True)
    embed.add_field(name="📅 Criado em", value=pedido["criado_em"].strftime("%d/%m/%Y %H:%M"),inline=True)
    await ctx.send(embed=embed)

@bot.command(name="falhas")
@commands.check(lambda ctx: any(r.id == CARGO_DONO for r in ctx.author.roles))
async def listar_falhas(ctx):
    """Admin: lista pedidos com falha de entrega."""
    falhas = await db_pedidos_falha_pendentes()
    if not falhas:
        return await ctx.send("✅ Nenhuma falha de entrega pendente.")

    embed = Embed(title=f"🔴 Falhas de Entrega ({len(falhas)})", color=Color.red(),
                  timestamp=datetime.now(timezone.utc))
    for p in falhas[:10]:
        embed.add_field(
            name=f"{p['produto_nome']}",
            value=f"👤 {p['user_tag']} · 🆔 `{p['id']}` · {p['criado_em'].strftime('%d/%m %H:%M')}",
            inline=False
        )
    embed.set_footer(text="Use !pedido <id> para detalhes ou o botão Reentrega no menu Admin.")
    await ctx.send(embed=embed)


# ================= EVENTS =================
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)

@bot.event
async def on_ready():
    try:
        bot.add_view(PainelPrincipal())
        for t in [atualizar_sistema, verificar_pagamentos_pendentes,
                  expirar_pedidos_antigos, reset_stats_diarias]:
            if not t.is_running():
                t.start()
        await atualizar_painel_privado()
        print(f"✅ Bot da Loja online: {bot.user}")
    except Exception as e:
        print(f"[ERRO on_ready] {e}")


# ================= MAIN =================
async def main():
    await init_db()
    await start_web()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())