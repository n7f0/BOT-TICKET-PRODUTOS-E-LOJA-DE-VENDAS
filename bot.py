# bot.py - CÓDIGO COMPLETO CORRIGIDO
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
import secrets
import aiofiles
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
cooldowns: dict = {}
COOLDOWN_SEGUNDOS = 60

UPLOAD_FOLDER = "uploads/produtos"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

SCHEMA_VERSION = 4

# ================= FUNÇÕES DO BANCO DE DADOS =================
async def init_db():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (versao INTEGER PRIMARY KEY)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS produtos (
                    id TEXT PRIMARY KEY, nome TEXT NOT NULL, preco NUMERIC(10,2) NOT NULL,
                    emoji TEXT DEFAULT '🛒', link TEXT NOT NULL, estoque INTEGER DEFAULT -1,
                    vendas INTEGER DEFAULT 0, criado_em TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pedidos (
                    id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, user_tag TEXT,
                    produto_id TEXT NOT NULL, produto_nome TEXT NOT NULL, produto_preco NUMERIC(10,2) NOT NULL,
                    status TEXT DEFAULT 'pendente', entregue BOOLEAN DEFAULT FALSE,
                    tentativas INTEGER DEFAULT 0, criado_em TIMESTAMPTZ DEFAULT NOW(),
                    atualizado_em TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("CREATE TABLE IF NOT EXISTS estatisticas (chave TEXT PRIMARY KEY, valor TEXT NOT NULL)")
            await conn.execute("CREATE TABLE IF NOT EXISTS painel_ids (nome TEXT PRIMARY KEY, msg_id BIGINT NOT NULL)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS downloads_temporarios (
                    key TEXT PRIMARY KEY, produto_id TEXT NOT NULL, user_id BIGINT NOT NULL,
                    arquivo_path TEXT NOT NULL, usado BOOLEAN DEFAULT FALSE,
                    criado_em TIMESTAMPTZ DEFAULT NOW(), expira_em TIMESTAMPTZ DEFAULT NOW() + INTERVAL '1 hour'
                )
            """)

            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('vendas','0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('faturamento','0.0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('vendas_hoje','0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('faturamento_hoje','0.0') ON CONFLICT (chave) DO NOTHING")
            await conn.execute("INSERT INTO estatisticas (chave, valor) VALUES ('ultima_reset','') ON CONFLICT (chave) DO NOTHING")

            row = await conn.fetchrow("SELECT versao FROM schema_version LIMIT 1")
            versao_atual = row["versao"] if row else 0
            if versao_atual < 4:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS downloads_temporarios (
                        key TEXT PRIMARY KEY, produto_id TEXT NOT NULL, user_id BIGINT NOT NULL,
                        arquivo_path TEXT NOT NULL, usado BOOLEAN DEFAULT FALSE,
                        criado_em TIMESTAMPTZ DEFAULT NOW(), expira_em TIMESTAMPTZ DEFAULT NOW() + INTERVAL '1 hour'
                    )
                """)
            await conn.execute("INSERT INTO schema_version (versao) VALUES ($1) ON CONFLICT (versao) DO UPDATE SET versao=$1", 4)
        print(f"✅ Banco de dados inicializado (schema v4).")
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
        await conn.execute("INSERT INTO produtos (id,nome,preco,emoji,link,estoque) VALUES ($1,$2,$3,$4,$5,$6)", pid, nome, preco, emoji, link, estoque)

async def db_editar_produto(pid, nome, preco, estoque):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE produtos SET nome=$2, preco=$3, estoque=$4 WHERE id=$1", pid, nome, preco, estoque)

async def db_remover_produto(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM produtos WHERE id=$1", pid)

async def db_produto_mais_vendido() -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM produtos ORDER BY vendas DESC LIMIT 1")
    return dict(row) if row else None

async def db_decrementar_estoque(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE produtos SET estoque = estoque - 1, vendas = vendas + 1 WHERE id=$1 AND estoque > 0", pid)

async def db_verificar_estoque(pid) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT estoque FROM produtos WHERE id=$1", pid)
    if not row:
        return False
    return row["estoque"] == -1 or row["estoque"] > 0

async def db_inserir_pedido(pid, user_id, user_tag, produto_id, produto_nome, produto_preco):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO pedidos (id,user_id,user_tag,produto_id,produto_nome,produto_preco,status,entregue,tentativas) VALUES ($1,$2,$3,$4,$5,$6,'pendente',FALSE,0)", pid, user_id, user_tag, produto_id, produto_nome, produto_preco)

async def db_buscar_pedido(pid) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM pedidos WHERE id=$1", pid)
    return dict(row) if row else None

async def db_marcar_entregue(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE pedidos SET status='aprovado', entregue=TRUE, tentativas=tentativas+1, atualizado_em=NOW() WHERE id=$1", pid)

async def db_marcar_falha_entrega(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE pedidos SET status='falha_entrega', tentativas=tentativas+1, atualizado_em=NOW() WHERE id=$1", pid)

async def db_marcar_expirado(pid):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE pedidos SET status='expirado', atualizado_em=NOW() WHERE id=$1", pid)

async def db_pedidos_usuario(user_id: int) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM pedidos WHERE user_id=$1 ORDER BY criado_em DESC LIMIT 10", user_id)
    return [dict(r) for r in rows]

async def db_pedidos_falha_pendentes() -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM pedidos WHERE status='falha_entrega' ORDER BY criado_em DESC")
    return [dict(r) for r in rows]

async def db_pedidos_pendentes_antigos(minutos: int = 35) -> list:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM pedidos WHERE status='pendente' AND criado_em < NOW() - ($1 * INTERVAL '1 minute')", minutos)
    return [dict(r) for r in rows]

async def db_get_stat(chave: str) -> float:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT valor FROM estatisticas WHERE chave=$1", chave)
    return float(row["valor"]) if row and row["valor"] else 0.0

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
        await conn.execute("UPDATE estatisticas SET valor=$1 WHERE chave='ultima_reset'", datetime.now(timezone.utc).isoformat())

async def db_get_painel_id(nome: str) -> int | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT msg_id FROM painel_ids WHERE nome=$1", nome)
    return row["msg_id"] if row else None

async def db_set_painel_id(nome: str, msg_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO painel_ids (nome,msg_id) VALUES ($1,$2) ON CONFLICT (nome) DO UPDATE SET msg_id=$2", nome, msg_id)

# ================= FUNÇÕES DE DOWNLOAD =================
def gerar_key_unica(produto_id: str, user_id: int) -> str:
    timestamp = datetime.now(timezone.utc).timestamp()
    random_part = secrets.token_hex(16)
    data = f"{produto_id}_{user_id}_{timestamp}_{random_part}"
    return hashlib.sha256(data.encode()).hexdigest()[:32]

async def salvar_key_no_banco(key: str, produto_id: str, user_id: int, arquivo_path: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO downloads_temporarios (key, produto_id, user_id, arquivo_path) VALUES ($1,$2,$3,$4)", key, produto_id, user_id, arquivo_path)

async def validar_key(key: str) -> dict | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM downloads_temporarios WHERE key = $1 AND usado = FALSE AND expira_em > NOW()", key)
        return dict(row) if row else None

async def marcar_key_como_usada(key: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE downloads_temporarios SET usado = TRUE WHERE key = $1", key)

async def obter_arquivo_produto(produto_id: str) -> str | None:
    for ext in ['.rar', '.zip', '.7z', '.exe', '.pdf']:
        arquivo_path = os.path.join(UPLOAD_FOLDER, f"{produto_id}{ext}")
        if os.path.exists(arquivo_path):
            return arquivo_path
    for arquivo in os.listdir(UPLOAD_FOLDER):
        if arquivo.startswith(produto_id):
            return os.path.join(UPLOAD_FOLDER, arquivo)
    return None

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
    if not secret or not header_signature:
        return True
    try:
        partes = dict(p.split("=", 1) for p in header_signature.split(","))
        ts, v1 = partes.get("ts", ""), partes.get("v1", "")
        msg = f"{ts}.{payload.decode('utf-8')}"
        calc = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc, v1)
    except:
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
    vendas, faturamento = await db_get_stat("vendas"), await db_get_stat("faturamento")
    vendas_hoje, fat_hoje = await db_get_stat("vendas_hoje"), await db_get_stat("faturamento_hoje")
    mais_vendido = await db_produto_mais_vendido()
    falhas = await db_pedidos_falha_pendentes()

    embed = Embed(title="📊 PAINEL DE VENDAS", color=Color.dark_gold(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="📦 Vendas Totais", value=str(int(vendas)), inline=True)
    embed.add_field(name="💰 Faturamento Total", value=f"R$ {formatar_preco(faturamento)}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="📅 Vendas Hoje", value=str(int(vendas_hoje)), inline=True)
    embed.add_field(name="💵 Faturamento Hoje", value=f"R$ {formatar_preco(fat_hoje)}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    if mais_vendido:
        embed.add_field(name="🏆 Produto Mais Vendido", value=f"{mais_vendido.get('emoji','🛒')} {mais_vendido['nome']} ({mais_vendido['vendas']} vendas)", inline=False)
    if falhas:
        embed.add_field(name="⚠️ Falhas de Entrega", value=f"{len(falhas)} pedido(s) não entregue(s)", inline=False)
    embed.set_footer(text="Atualizado a cada 2 min")
    return embed

async def montar_embed_loja():
    produtos = await db_listar_produtos()
    embed = Embed(title="✨ **NEXZY STORE** ✨", description="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n**🎉 A MELHOR EXPERIÊNCIA DE COMPRAS 🎉**\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n```fix\n✔️ Pagamento via PIX (Instantâneo)\n✔️ Entrega automática na DM\n✔️ Suporte 24/7\n✔️ 100% Seguro e Confiável```\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", color=0x5865F2)
    embed.set_image(url="https://media.discordapp.net/attachments/1491808878562643998/1491808965170958396/e6876514-c5ae-477f-a84b-d7b7db0c01e5.png")
    embed.set_footer(text="⭐ Nexzy Store • A Loja Oficial ⭐")
    embed.timestamp = datetime.now(timezone.utc)
    if not produtos:
        embed.add_field(name="📢 **SEM PRODUTOS**", value="```diff\n- Nenhum produto cadastrado ainda!\n+ Aguarde novidades em breve...```", inline=False)
        return embed
    for pid, prod in produtos.items():
        estoque_texto = "∞" if prod["estoque"] == -1 else str(prod["estoque"])
        embed.add_field(name=f"{prod.get('emoji', '🛒')} **{prod['nome']}**", value=f"```ml\n💰 Preço: R$ {formatar_preco(prod['preco'])}\n📦 Estoque: {estoque_texto}\n🆔 ID: {pid}\n```", inline=True)
    embed.add_field(name="━━━━━━━━━━━━━━━━━━━━", value="**📌 COMO COMPRAR?**\n```\n1️⃣ Clique no botão COMPRAR\n2️⃣ Escolha seu produto\n3️⃣ Efetue o PIX\n4️⃣ Receba sua KEY na DM```\n💬 **Precisa de ajuda?** Contate um administrador!", inline=False)
    return embed

async def montar_embed_admin():
    produtos = await db_listar_produtos()
    embed = Embed(title="👑 Painel Admin", color=Color.purple())
    for pid, prod in produtos.items():
        tem_arquivo = "📁" if await obter_arquivo_produto(pid) else "❌"
        embed.add_field(name=f"{prod.get('emoji','🛒')} {prod['nome']}", value=f"ID: `{pid}`\nPreço: R$ {formatar_preco(prod['preco'])}\nEstoque: {prod['estoque'] if prod['estoque'] != -1 else '∞'}\nVendas: {prod['vendas']}\nArquivo: {tem_arquivo}", inline=True)
    return embed

# ================= MODAIS =================
class AdicionarProdutoModal(discord.ui.Modal, title="✨ Adicionar Novo Produto"):
    produto_id = discord.ui.TextInput(label="🆔 ID do Produto", placeholder="Ex: vip1, vip_premium", required=True, max_length=50)
    nome = discord.ui.TextInput(label="📦 Nome do Produto", placeholder="Ex: VIP Premium", required=True, max_length=100)
    preco = discord.ui.TextInput(label="💰 Preço (R$)", placeholder="Ex: 49.90", required=True, max_length=10)
    emoji = discord.ui.TextInput(label="😀 Emoji do Produto", placeholder="Ex: 👑, 🎁, 💎", required=False, default="🛒", max_length=5)
    link_alternativo = discord.ui.TextInput(label="🔗 Link Alternativo (opcional)", placeholder="Só use se não tiver arquivo .rar", required=False, max_length=200)
    estoque = discord.ui.TextInput(label="📊 Estoque (-1 = Ilimitado)", placeholder="-1", required=False, default="-1", max_length=10)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_float = float(self.preco.value.replace(",", "."))
            estoque_int = int(self.estoque.value) if self.estoque.value else -1
            await db_adicionar_produto(self.produto_id.value, self.nome.value, preco_float, self.emoji.value or "🛒", self.link_alternativo.value or "", estoque_int)
            await atualizar_painel_loja()
            await atualizar_painel_vendas()
            embed = Embed(title="✅ Produto Adicionado!", color=Color.green())
            embed.add_field(name="📦 Nome", value=self.nome.value, inline=True)
            embed.add_field(name="💰 Preço", value=f"R$ {formatar_preco(preco_float)}", inline=True)
            embed.add_field(name="😀 Emoji", value=self.emoji.value or "🛒", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro: {e}", ephemeral=True)

class UploadRarModal(discord.ui.Modal, title="📤 Upload de Arquivo .RAR"):
    produto_id = discord.ui.TextInput(label="🆔 ID do Produto", placeholder="Ex: vip1 (mesmo ID do produto)", required=True, max_length=50)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"📤 Agora envie o arquivo **.RAR** para o produto `{self.produto_id.value}`\nVocê tem 60 segundos.", ephemeral=True)
        def check(msg):
            return msg.author == interaction.user and msg.attachments and msg.attachments[0].filename.lower().endswith(('.rar', '.zip', '.7z'))
        try:
            msg = await bot.wait_for('message', timeout=60, check=check)
            attachment = msg.attachments[0]
            arquivo_bytes = await attachment.read()
            extensao = os.path.splitext(attachment.filename)[1]
            caminho = os.path.join(UPLOAD_FOLDER, f"{self.produto_id.value}{extensao}")
            async with aiofiles.open(caminho, 'wb') as f:
                await f.write(arquivo_bytes)
            await interaction.followup.send(f"✅ **Arquivo salvo!**\n📁 Produto: `{self.produto_id.value}`\n📄 Arquivo: `{attachment.filename}`", ephemeral=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ **Tempo esgotado!**", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ **Erro:** {e}", ephemeral=True)

class EditarProdutoModal(discord.ui.Modal, title="✏️ Editar Produto"):
    def __init__(self, produto_id: str):
        super().__init__()
        self.produto_id = produto_id
        self.add_item(discord.ui.TextInput(label="📦 Nome do Produto", placeholder="Nome do produto", required=True))
        self.add_item(discord.ui.TextInput(label="💰 Preço (R$)", placeholder="49.90", required=True))
        self.add_item(discord.ui.TextInput(label="📊 Estoque (-1 = Ilimitado)", placeholder="-1", required=False, default="-1"))
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            nome = self.children[0].value
            preco = float(self.children[1].value.replace(",", "."))
            estoque = int(self.children[2].value) if self.children[2].value else -1
            await db_editar_produto(self.produto_id, nome, preco, estoque)
            await interaction.response.send_message(f"✅ Produto `{self.produto_id}` editado!", ephemeral=True)
            await atualizar_painel_loja()
            await atualizar_painel_vendas()
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro: {e}", ephemeral=True)

# ================= VIEWS =================
class SelecionarProduto(discord.ui.Select):
    def __init__(self, produtos: dict):
        options = []
        for pid, prod in produtos.items():
            if prod["estoque"] == -1 or prod["estoque"] > 0:
                options.append(discord.SelectOption(label=f"{prod['nome']} - R$ {formatar_preco(prod['preco'])}", value=pid, emoji=prod.get('emoji', '🛒'), description=f"ID: {pid}"))
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
            return await interaction.response.send_message("❌ Nenhum produto disponível.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(SelecionarProduto(disponiveis))
        await interaction.response.send_message("Selecione o produto:", view=view, ephemeral=True)
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
    
    @discord.ui.button(label="➕ Adicionar Produto", style=discord.ButtonStyle.success, emoji="➕")
    async def add_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdicionarProdutoModal())
    
    @discord.ui.button(label="📤 Upload .RAR", style=discord.ButtonStyle.primary, emoji="📤")
    async def upload_rar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(UploadRarModal())
    
    @discord.ui.button(label="✏️ Editar Produto", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db_listar_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        select = discord.ui.Select(placeholder="🔍 Selecione um produto para editar...")
        for pid, prod in list(produtos.items())[:25]:
            select.add_option(label=f"{prod['nome'][:50]}", value=pid, emoji=prod.get('emoji', '🛒'), description=f"R$ {formatar_preco(prod['preco'])}")
        async def select_callback(interaction: discord.Interaction):
            await interaction.response.send_modal(EditarProdutoModal(select.values[0]))
        select.callback = select_callback
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message("📋 Selecione o produto:", view=view, ephemeral=True)
    
    @discord.ui.button(label="🗑️ Remover Produto", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_produto(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db_listar_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        select = discord.ui.Select(placeholder="⚠️ Selecione o produto para remover...")
        for pid, prod in list(produtos.items())[:25]:
            select.add_option(label=f"{prod['nome'][:50]}", value=pid, emoji=prod.get('emoji', '🛒'), description=f"R$ {formatar_preco(prod['preco'])}")
        async def select_callback(interaction: discord.Interaction):
            produto_id = select.values[0]
            await db_remover_produto(produto_id)
            await interaction.response.send_message(f"✅ Produto `{produto_id}` removido!", ephemeral=True)
            await atualizar_painel_loja()
            await atualizar_painel_vendas()
        select.callback = select_callback
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message("🗑️ Selecione o produto:", view=view, ephemeral=True)
    
    @discord.ui.button(label="📊 Estatísticas", style=discord.ButtonStyle.secondary, emoji="📊")
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=await montar_embed_vendas(), ephemeral=True)

# ================= COMPRA E ENTREGA =================
async def processar_compra(interaction: discord.Interaction, key: str):
    restante = verificar_cooldown(interaction.user.id)
    if restante > 0:
        return await interaction.response.send_message(f"⏳ Aguarde **{restante}s**", ephemeral=True)
    produtos = await db_listar_produtos()
    produto = produtos.get(key)
    if not produto:
        return await interaction.response.send_message("❌ Produto não encontrado.", ephemeral=True)
    if not await db_verificar_estoque(key):
        return await interaction.response.send_message("❌ Produto sem estoque.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        payment_data = sdk.payment().create({"transaction_amount": float(produto["preco"]), "description": produto["nome"], "payment_method_id": "pix", "payer": {"email": f"user_{interaction.user.id}@email.com"}})
        response = payment_data["response"]
        if "point_of_interaction" not in response:
            return await interaction.followup.send("❌ Erro ao gerar PIX.", ephemeral=True)
        pix_qr_code = response["point_of_interaction"]["transaction_data"]["qr_code"]
        pix_copy_paste = response["point_of_interaction"]["transaction_data"]["qr_code_base64"]
        payment_id = response["id"]
        pedido_id = str(uuid.uuid4())
        await db_inserir_pedido(pedido_id, interaction.user.id, str(interaction.user), key, produto["nome"], produto["preco"])
        pedidos_pendentes[payment_id] = pedido_id
        embed = Embed(title="💳 Pagamento PIX", description=f"**Produto:** {produto['nome']}\n**Valor:** R$ {formatar_preco(produto['preco'])}", color=Color.green())
        embed.add_field(name="📱 Código PIX", value=f"```\n{pix_copy_paste}\n```", inline=False)
        embed.set_image(url=pix_qr_code)
        embed.set_footer(text=f"ID: {pedido_id} | Expira em 30 min")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="✅ Já paguei", style=discord.ButtonStyle.success, custom_id=f"check_{payment_id}"))
        view.add_item(discord.ui.Button(label="❌ Cancelar", style=discord.ButtonStyle.danger, custom_id=f"cancel_{payment_id}"))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        registrar_cooldown(interaction.user.id)
        asyncio.create_task(verificar_pagamento(payment_id, pedido_id, interaction.user, produto, key))
    except Exception as e:
        await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

async def verificar_pagamento(payment_id, pedido_id, user, produto, produto_key):
    for _ in range(60):
        await asyncio.sleep(30)
        try:
            payment_info = sdk.payment().get(payment_id)
            status = payment_info["response"].get("status")
            if status == "approved":
                if await entregar_com_key(user, produto, produto_key, pedido_id):
                    await db_marcar_entregue(pedido_id)
                    await db_decrementar_estoque(produto_key)
                    await db_incrementar_venda(float(produto["preco"]))
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

async def entregar_com_key(user, produto, produto_id, pedido_id) -> bool:
    key = gerar_key_unica(produto_id, user.id)
    arquivo_path = await obter_arquivo_produto(produto_id)
    if not arquivo_path:
        return False
    await salvar_key_no_banco(key, produto_id, user.id, arquivo_path)
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", os.getenv("RAILWAY_STATIC_URL", "localhost:8080"))
    download_url = f"https://{railway_domain}/download/{key}"
    embed = Embed(title="✅ **COMPRA APROVADA!**", description=f"**{produto['nome']}** - R$ {formatar_preco(produto['preco'])}", color=Color.green(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="🔑 **SUA KEY ÚNICA**", value=f"```\n{key}\n```\n⚠️ **Guarde esta key!** Ela só funciona uma vez.", inline=False)
    embed.add_field(name="📥 **LINK PARA DOWNLOAD**", value=f"**[CLIQUE AQUI PARA BAIXAR]({download_url})**\n\n```diff\n- O link expira APÓS o download\n- Expira em 1 hora\n- Só pode ser usado UMA vez\n```", inline=False)
    embed.add_field(name="📋 **COMO BAIXAR:**", value="1. Clique no link\n2. Cole sua KEY\n3. Clique em 'Verificar e Baixar'\n4. O download começará", inline=False)
    embed.set_footer(text="🔒 Link seguro • Download único")
    try:
        await user.send(embed=embed)
        return True
    except:
        return False

# ================= WEBHOOKS =================
async def download_page_handler(request):
    key = request.match_info.get('key')
    if not key:
        return web.Response(status=400, text="Key não fornecida")
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><title>Nexzy Store - Download</title>
    <style>
        body {{ font-family: Arial; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; display: flex; justify-content: center; align-items: center; }}
        .container {{ background: white; border-radius: 20px; padding: 40px; max-width: 500px; text-align: center; }}
        input {{ width: 100%; padding: 12px; margin: 10px 0; border: 2px solid #ddd; border-radius: 8px; }}
        button {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; padding: 12px 30px; border-radius: 8px; cursor: pointer; }}
        .error {{ color: red; }}
    </style>
    </head>
    <body>
    <div class="container">
        <h1>📦 Nexzy Store</h1>
        <p>Digite sua KEY para baixar o produto</p>
        <input type="text" id="keyInput" placeholder="Cole sua KEY aqui">
        <button onclick="baixar()">✅ Verificar e Baixar</button>
        <div id="msg" class="error"></div>
    </div>
    <script>
        async function baixar() {{
            const key = document.getElementById('keyInput').value;
            if (!key) {{ document.getElementById('msg').innerText = 'Digite sua KEY!'; return; }}
            try {{
                const res = await fetch(`/api/download/${{key}}`);
                const data = await res.json();
                if (res.ok) {{ window.location.href = data.download_url; }}
                else {{ document.getElementById('msg').innerText = data.error; }}
            }} catch(e) {{ document.getElementById('msg').innerText = 'Erro!'; }}
        }}
    </script>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def download_api_handler(request):
    key = request.match_info.get('key')
    if not key:
        return web.json_response({"error": "Key não fornecida"}, status=400)
    key_info = await validar_key(key)
    if not key_info:
        return web.json_response({"error": "Key inválida, expirada ou já usada!"}, status=403)
    await marcar_key_como_usada(key)
    arquivo_path = key_info['arquivo_path']
    if not os.path.exists(arquivo_path):
        return web.json_response({"error": "Arquivo não encontrado!"}, status=404)
    return web.json_response({"success": True, "download_url": f"/files/{os.path.basename(arquivo_path)}"})

async def serve_file_handler(request):
    filename = request.match_info.get('filename')
    for root, dirs, files in os.walk(UPLOAD_FOLDER):
        if filename in files:
            file_path = os.path.join(root, filename)
            return web.FileResponse(file_path, headers={'Content-Disposition': f'attachment; filename="{filename}"'})
    return web.Response(status=404, text="Arquivo não encontrado")

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
                        if produto and await entregar_com_key(user, produto, pedido["produto_id"], pedido_id):
                            await db_marcar_entregue(pedido_id)
                            await db_decrementar_estoque(pedido["produto_id"])
                            await db_incrementar_venda(pedido["produto_preco"])
                            await atualizar_painel_vendas()
                            await atualizar_painel_loja()
                        else:
                            await db_marcar_falha_entrega(pedido_id)
        return web.Response(status=200, text="OK")
    except Exception as e:
        print(f"Erro: {e}")
        return web.Response(status=500, text="Erro")

async def start_webhook():
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    app.router.add_get("/download/{key}", download_page_handler)
    app.router.add_get("/api/download/{key}", download_api_handler)
    app.router.add_get("/files/{filename}", serve_file_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    print(f"✅ Webhook rodando na porta {os.getenv('PORT', '8080')}")

# ================= ATUALIZAR PAINÉIS =================
async def atualizar_painel_vendas():
    canal = bot.get_channel(CANAL_VENDAS)
    if not canal:
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
    for pedido in await db_pedidos_pendentes_antigos(35):
        await db_marcar_expirado(pedido["id"])

@tasks.loop(hours=24)
async def reset_stats_diario():
    await db_reset_stats_diarias()

# ================= COMANDOS =================
@bot.command(name="loja")
async def cmd_loja(ctx):
    produtos = await db_listar_produtos()
    if not produtos:
        return await ctx.send("❌ Nenhum produto cadastrado! Use o botão Admin.")
    await ctx.send(embed=await montar_embed_loja(), view=PainelPrincipal())
    await ctx.message.delete()

@bot.command(name="vendas")
async def cmd_vendas(ctx):
    await ctx.send(embed=await montar_embed_vendas())
    await ctx.message.delete()

@bot.command(name="testar")
async def testar_config(ctx):
    embed = Embed(title="🔧 Teste", color=Color.blue())
    embed.add_field(name="🛒 Loja", value=f"{bot.get_channel(CANAL_LOJA).mention if bot.get_channel(CANAL_LOJA) else '❌'}", inline=False)
    embed.add_field(name="📊 Vendas", value=f"{bot.get_channel(CANAL_VENDAS).mention if bot.get_channel(CANAL_VENDAS) else '❌'}", inline=False)
    embed.add_field(name="📦 Produtos", value=str(len(await db_listar_produtos())), inline=False)
    embed.add_field(name="📁 Arquivos", value=str(len(os.listdir(UPLOAD_FOLDER)) if os.path.exists(UPLOAD_FOLDER) else 0), inline=False)
    await ctx.send(embed=embed)
    await ctx.message.delete()

# ================= EVENTOS =================
@bot.event
async def on_ready():
    print(f"✅ Bot logado como {bot.user}")
    print(f"🛒 Loja: {CANAL_LOJA}")
    print(f"📊 Vendas: {CANAL_VENDAS}")
    await init_db()
    if db_pool is None:
        print("❌ Banco não conectado!")
        return
    produtos = await db_listar_produtos()
    if not produtos:
        print("📦 Criando produtos padrão...")
        for pid, nome, preco, emoji, link, estoque in [("vip1", "VIP Bronze", 19.90, "🥉", "", -1), ("vip2", "VIP Prata", 39.90, "🥈", "", -1), ("vip3", "VIP Ouro", 69.90, "🥇", "", -1)]:
            await db_adicionar_produto(pid, nome, preco, emoji, link, estoque)
    await atualizar_painel_loja()
    await atualizar_painel_vendas()
    atualizar_paineis.start()
    verificar_pedidos_expirados.start()
    reset_stats_diario.start()
    asyncio.create_task(start_webhook())
    print(f"✅ Bot pronto! Use !loja")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("check_"):
            payment_id = int(custom_id.split("_")[1])
            await interaction.response.send_message("⏳ Verificando...", ephemeral=True)
            try:
                payment_info = sdk.payment().get(payment_id)
                if payment_info["response"].get("status") == "approved":
                    await interaction.edit_original_response(content="✅ Pagamento aprovado! Verifique sua DM.", embed=None, view=None)
                else:
                    await interaction.edit_original_response(content="⏳ Pagamento não identificado ainda.", embed=None, view=None)
            except:
                await interaction.edit_original_response(content="❌ Erro ao verificar.", embed=None, view=None)
        elif custom_id.startswith("cancel_"):
            await interaction.response.send_message("❌ Pedido cancelado.", ephemeral=True)

# ================= MAIN =================
async def start_bot():
    try:
        await bot.start(DISCORD_TOKEN)
    except discord.errors.HTTPException as e:
        if e.status == 429:
            print("Rate limit, aguardando 30s...")
            await asyncio.sleep(30)
            await start_bot()
        else:
            raise e

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_bot())
    except KeyboardInterrupt:
        print("Bot desligado")
    finally:
        loop.close()