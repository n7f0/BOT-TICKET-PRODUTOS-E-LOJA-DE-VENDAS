# bot.py - NEXZY STORE - VERSÃO PROFISSIONAL
import discord
from discord.ext import commands
from discord import Embed, Color
import aiohttp
import mercadopago
import uuid
import asyncio
import os
import asyncpg
import secrets
import random
import string
import hashlib
from datetime import datetime, timedelta
from aiohttp import web

# ================= CONFIG =================
CARGO_DONO = int(os.getenv("CARGO_DONO", "0"))
CANAL_LOJA = int(os.getenv("CANAL_LOJA", "0"))
CANAL_VENDAS = int(os.getenv("CANAL_VENDAS", "0"))
DISCORD_TOKEN = os.getenv("LOJA_DISCORD_TOKEN")
MP_TOKEN = os.getenv("MERCADO_PAGO_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_LOG_URL = "https://discord.com/api/webhooks/1494018278593663218/thPR-PptRQNKQKvY0Jw9aFVFf7lkxhPp00Bb7Dn2_Ee7nfwFIP2ZOmr7NO6ApAH-H7ts"
BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")  # URL pública do Railway

if not DISCORD_TOKEN:
    print("❌ Token do Discord não configurado!")
    exit(1)

if not MP_TOKEN:
    print("❌ Token do Mercado Pago não configurado!")
    exit(1)

if not DATABASE_URL:
    print("❌ DATABASE_URL não configurada!")
    exit(1)

if "railwaypostgresql://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("railwaypostgresql://", "postgresql://")

sdk = mercadopago.SDK(MP_TOKEN)
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

db = None
pedidos_pendentes = {}

def gerar_id_aleatorio():
    caracteres = string.ascii_lowercase + string.digits
    return ''.join(random.choices(caracteres, k=6))

def gerar_key_unica():
    """Gera uma key única no formato NEXZY-XXXX-XXXX-XXXX-XXXX"""
    partes = [''.join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(4)]
    return f"NEXZY-{'-'.join(partes)}"

def gerar_token_download():
    """Gera token seguro para link temporário"""
    return secrets.token_urlsafe(32)

# ================= BANCO DE DADOS =================
async def init_db():
    global db
    try:
        db = await asyncpg.create_pool(DATABASE_URL)
        async with db.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS produtos (
                    id TEXT PRIMARY KEY,
                    nome TEXT NOT NULL,
                    preco REAL NOT NULL,
                    emoji TEXT DEFAULT '🛒',
                    descricao TEXT DEFAULT ''
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pedidos (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    produto_id TEXT NOT NULL,
                    produto_nome TEXT NOT NULL,
                    produto_preco REAL NOT NULL,
                    status TEXT DEFAULT 'pendente',
                    criado_em TIMESTAMP DEFAULT NOW()
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vendas (
                    id SERIAL PRIMARY KEY,
                    total REAL DEFAULT 0,
                    quantidade INTEGER DEFAULT 0
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS keys_entregues (
                    id SERIAL PRIMARY KEY,
                    pedido_id TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    produto_nome TEXT NOT NULL,
                    key_valor TEXT NOT NULL UNIQUE,
                    download_token TEXT UNIQUE,
                    token_expira_em TIMESTAMP,
                    token_usado BOOLEAN DEFAULT FALSE,
                    criado_em TIMESTAMP DEFAULT NOW()
                )
            """)

            await conn.execute("INSERT INTO vendas (id, total, quantidade) VALUES (1, 0, 0) ON CONFLICT (id) DO NOTHING")

            # Adiciona coluna descricao se não existir (migração segura)
            try:
                await conn.execute("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS descricao TEXT DEFAULT ''")
            except Exception:
                pass

            existing = await conn.fetch("SELECT * FROM produtos")
            if not existing:
                await conn.execute("INSERT INTO produtos (id, nome, preco, emoji, descricao) VALUES ('prod1', 'VIP Bronze', 19.90, '🥉', 'Acesso VIP Bronze por 30 dias')")
                await conn.execute("INSERT INTO produtos (id, nome, preco, emoji, descricao) VALUES ('prod2', 'VIP Prata', 39.90, '🥈', 'Acesso VIP Prata por 30 dias')")
                await conn.execute("INSERT INTO produtos (id, nome, preco, emoji, descricao) VALUES ('prod3', 'VIP Ouro', 69.90, '🥇', 'Acesso VIP Ouro por 30 dias')")
                print("✅ Produtos exemplo criados")

        print("✅ Banco conectado!")
        return True
    except Exception as e:
        print(f"❌ Erro banco: {e}")
        return False

async def get_produtos():
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT id, nome, preco, emoji, descricao FROM produtos")
        return {r["id"]: {"id": r["id"], "nome": r["nome"], "preco": r["preco"], "emoji": r["emoji"], "descricao": r["descricao"] or ""} for r in rows}

async def add_produto(pid, nome, preco, emoji, descricao=""):
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO produtos (id, nome, preco, emoji, descricao) VALUES ($1,$2,$3,$4,$5)", pid, nome, preco, emoji, descricao)

async def remove_produto(pid):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM produtos WHERE id=$1", pid)

async def edit_produto(pid, nome, preco, emoji, descricao):
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE produtos SET nome=$2, preco=$3, emoji=$4, descricao=$5 WHERE id=$1",
            pid, nome, preco, emoji, descricao
        )

async def add_pedido(pid, user_id, produto_id, nome, preco):
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO pedidos (id, user_id, produto_id, produto_nome, produto_preco) VALUES ($1,$2,$3,$4,$5)", pid, user_id, produto_id, nome, preco)

async def update_pedido(pid, status):
    async with db.acquire() as conn:
        await conn.execute("UPDATE pedidos SET status=$1 WHERE id=$2", status, pid)

async def get_vendas():
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT total, quantidade FROM vendas WHERE id=1")
        return row["total"], row["quantidade"]

async def add_venda(valor):
    async with db.acquire() as conn:
        await conn.execute("UPDATE vendas SET total = total + $1, quantidade = quantidade + 1 WHERE id=1", valor)

async def salvar_key(pedido_id, user_id, produto_nome, key_valor, download_token, expira_em):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO keys_entregues (pedido_id, user_id, produto_nome, key_valor, download_token, token_expira_em) VALUES ($1,$2,$3,$4,$5,$6)",
            pedido_id, user_id, produto_nome, key_valor, download_token, expira_em
        )

async def marcar_token_usado(token):
    async with db.acquire() as conn:
        await conn.execute("UPDATE keys_entregues SET token_usado=TRUE WHERE download_token=$1", token)

async def get_key_por_token(token):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM keys_entregues WHERE download_token=$1", token)

def formatar_preco(valor):
    return f"R$ {float(valor):.2f}".replace(".", ",")

# ================= WEBHOOK LOG =================
async def log_webhook(titulo, descricao, cor=0x00ff99, campos=None):
    """Envia log bonito pro webhook do Discord"""
    try:
        embed = {
            "title": titulo,
            "description": descricao,
            "color": cor,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "NEXZY STORE • Sistema de Logs"},
            "fields": campos or []
        }
        payload = {"embeds": [embed]}
        async with aiohttp.ClientSession() as session:
            await session.post(WEBHOOK_LOG_URL, json=payload)
    except Exception as e:
        print(f"Erro webhook log: {e}")

# ================= MODALS =================
class ProdutoModal(discord.ui.Modal, title="✨ Adicionar Produto"):
    nome_input = discord.ui.TextInput(label="📦 Nome", placeholder="Ex: VIP Premium", required=True)
    preco_input = discord.ui.TextInput(label="💰 Preço (ex: 49.90)", placeholder="49.90", required=True)
    emoji_input = discord.ui.TextInput(label="😀 Emoji", placeholder="👑", required=False, default="🛒")
    descricao_input = discord.ui.TextInput(label="📝 Descrição", placeholder="Breve descrição do produto", required=False, style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            produto_id = gerar_id_aleatorio()
            nome = self.nome_input.value
            preco = float(self.preco_input.value.replace(",", "."))
            emoji = self.emoji_input.value or "🛒"
            descricao = self.descricao_input.value or ""

            produtos = await get_produtos()
            while produto_id in produtos:
                produto_id = gerar_id_aleatorio()

            await add_produto(produto_id, nome, preco, emoji, descricao)

            embed = Embed(title="✅ Produto Adicionado!", color=Color.green())
            embed.add_field(name="🆔 ID", value=f"`{produto_id}`", inline=False)
            embed.add_field(name="📦 Nome", value=nome, inline=True)
            embed.add_field(name="💰 Preço", value=formatar_preco(preco), inline=True)
            embed.add_field(name="😀 Emoji", value=emoji, inline=True)
            if descricao:
                embed.add_field(name="📝 Descrição", value=descricao, inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
            await atualizar_loja()

            await log_webhook(
                "📦 Produto Adicionado",
                f"Um novo produto foi cadastrado na loja.",
                cor=0x00ff99,
                campos=[
                    {"name": "Nome", "value": nome, "inline": True},
                    {"name": "Preço", "value": formatar_preco(preco), "inline": True},
                    {"name": "ID", "value": f"`{produto_id}`", "inline": True},
                    {"name": "Admin", "value": f"<@{interaction.user.id}>", "inline": True}
                ]
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)


class EditarProdutoModal(discord.ui.Modal, title="✏️ Editar Produto"):
    def __init__(self, produto):
        super().__init__()
        self.produto_id = produto["id"]
        self.nome_input = discord.ui.TextInput(label="📦 Nome", default=produto["nome"], required=True)
        self.preco_input = discord.ui.TextInput(label="💰 Preço", default=str(produto["preco"]), required=True)
        self.emoji_input = discord.ui.TextInput(label="😀 Emoji", default=produto["emoji"], required=False)
        self.descricao_input = discord.ui.TextInput(label="📝 Descrição", default=produto.get("descricao", ""), required=False, style=discord.TextStyle.short)
        self.add_item(self.nome_input)
        self.add_item(self.preco_input)
        self.add_item(self.emoji_input)
        self.add_item(self.descricao_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            nome = self.nome_input.value
            preco = float(self.preco_input.value.replace(",", "."))
            emoji = self.emoji_input.value or "🛒"
            descricao = self.descricao_input.value or ""

            await edit_produto(self.produto_id, nome, preco, emoji, descricao)

            embed = Embed(title="✅ Produto Editado!", color=Color.blue())
            embed.add_field(name="📦 Nome", value=nome, inline=True)
            embed.add_field(name="💰 Preço", value=formatar_preco(preco), inline=True)
            embed.add_field(name="😀 Emoji", value=emoji, inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
            await atualizar_loja()

            await log_webhook(
                "✏️ Produto Editado",
                f"Um produto foi modificado.",
                cor=0x3498db,
                campos=[
                    {"name": "Nome", "value": nome, "inline": True},
                    {"name": "Preço", "value": formatar_preco(preco), "inline": True},
                    {"name": "ID", "value": f"`{self.produto_id}`", "inline": True},
                    {"name": "Admin", "value": f"<@{interaction.user.id}>", "inline": True}
                ]
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)


# ================= SELECTS =================
class RemoverSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = [
            discord.SelectOption(label=f"{prod['nome']} ({pid})", value=pid, emoji=prod['emoji'])
            for pid, prod in produtos.items()
        ]
        super().__init__(placeholder="🗑️ Selecione o produto para remover", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        produtos = await get_produtos()
        produto = produtos.get(self.values[0])
        nome = produto["nome"] if produto else self.values[0]
        await remove_produto(self.values[0])
        await interaction.followup.send(f"✅ Produto **{nome}** removido com sucesso!", ephemeral=True)
        await atualizar_loja()

        await log_webhook(
            "🗑️ Produto Removido",
            f"Um produto foi removido da loja.",
            cor=0xe74c3c,
            campos=[
                {"name": "Nome", "value": nome, "inline": True},
                {"name": "ID", "value": f"`{self.values[0]}`", "inline": True},
                {"name": "Admin", "value": f"<@{interaction.user.id}>", "inline": True}
            ]
        )


class EditarSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = [
            discord.SelectOption(label=f"{prod['nome']} - {formatar_preco(prod['preco'])}", value=pid, emoji=prod['emoji'])
            for pid, prod in produtos.items()
        ]
        super().__init__(placeholder="✏️ Selecione o produto para editar", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        produtos = await get_produtos()
        produto = produtos.get(self.values[0])
        if not produto:
            return await interaction.response.send_message("❌ Produto não encontrado", ephemeral=True)
        await interaction.response.send_modal(EditarProdutoModal(produto))


class ProdutoSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = [
            discord.SelectOption(label=f"{prod['nome']} — {formatar_preco(prod['preco'])}", value=pid, emoji=prod['emoji'])
            for pid, prod in produtos.items()
        ]
        super().__init__(placeholder="🛒 Escolha um produto", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await iniciar_pagamento(interaction, self.values[0])


# ================= VIEWS =================
class LojaButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 Comprar", style=discord.ButtonStyle.success, emoji="🛒")
    async def comprar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await get_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto disponível no momento.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(ProdutoSelect(produtos))
        await interaction.response.send_message("📦 **Selecione o produto desejado:**", view=view, ephemeral=True)

    @discord.ui.button(label="👑 Admin", style=discord.ButtonStyle.danger, emoji="⚙️")
    async def admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == CARGO_DONO for r in interaction.user.roles):
            return await interaction.response.send_message("❌ Você não tem permissão para isso.", ephemeral=True)

        embed = Embed(
            title="⚙️ Painel Administrativo",
            description="Gerencie os produtos da **NEXZY STORE**",
            color=Color.dark_red()
        )
        embed.add_field(name="➕ Adicionar", value="Cadastra um novo produto", inline=True)
        embed.add_field(name="✏️ Editar", value="Altera nome, preço ou emoji", inline=True)
        embed.add_field(name="🗑️ Remover", value="Remove produto da loja", inline=True)
        embed.add_field(name="🧪 Teste", value="Simula uma compra (DM)", inline=True)
        embed.set_footer(text="NEXZY STORE — Painel Admin")

        view = AdminView()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="➕ Adicionar", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ProdutoModal())

    @discord.ui.button(label="✏️ Editar", style=discord.ButtonStyle.primary)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await get_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(EditarSelect(produtos))
        await interaction.response.send_message("✏️ **Selecione o produto para editar:**", view=view, ephemeral=True)

    @discord.ui.button(label="🗑️ Remover", style=discord.ButtonStyle.danger)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await get_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto cadastrado.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(RemoverSelect(produtos))
        await interaction.response.send_message("🗑️ **Selecione o produto para remover:**", view=view, ephemeral=True)

    @discord.ui.button(label="🧪 Teste de Compra", style=discord.ButtonStyle.secondary)
    async def teste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        produtos = await get_produtos()
        if not produtos:
            return await interaction.followup.send("❌ Nenhum produto para testar.", ephemeral=True)

        produto = list(produtos.values())[0]
        pedido_id = f"TESTE-{str(uuid.uuid4())[:8]}"

        key, link, expira = await gerar_entrega(pedido_id, interaction.user.id, produto["nome"])

        embed = Embed(
            title="🧪 SIMULAÇÃO DE COMPRA",
            description=f"Esta é uma simulação. Nenhum pagamento foi processado.",
            color=Color.orange()
        )
        embed.add_field(name="📦 Produto", value=produto["nome"], inline=True)
        embed.add_field(name="💰 Valor", value=formatar_preco(produto["preco"]), inline=True)
        embed.add_field(name="🔑 Key Gerada", value=f"```\n{key}\n```", inline=False)
        embed.add_field(name="🔗 Link de Download", value=f"[Clique aqui]({link})", inline=False)
        embed.add_field(name="⏰ Expira em", value=f"<t:{int(expira.timestamp())}:R>", inline=False)
        embed.set_footer(text="NEXZY STORE — Teste de Entrega")

        await interaction.user.send(embed=embed)
        await interaction.followup.send("✅ Simulação enviada para sua DM!", ephemeral=True)

        await log_webhook(
            "🧪 Teste de Compra Executado",
            "Uma simulação de compra foi realizada.",
            cor=0xf39c12,
            campos=[
                {"name": "Admin", "value": f"<@{interaction.user.id}>", "inline": True},
                {"name": "Produto", "value": produto["nome"], "inline": True},
                {"name": "Key", "value": f"`{key}`", "inline": False}
            ]
        )


# ================= ENTREGA =================
async def gerar_entrega(pedido_id, user_id, produto_nome):
    """Gera key única + link temporário (expira após download ou 1h)"""
    key = gerar_key_unica()
    token = gerar_token_download()
    expira_em = datetime.utcnow() + timedelta(hours=1)
    link = f"{BASE_URL}/download/{token}"

    await salvar_key(pedido_id, user_id, produto_nome, key, token, expira_em)
    return key, link, expira_em


async def entregar_produto(user, produto, pedido_id):
    key, link, expira_em = await gerar_entrega(pedido_id, user.id, produto["nome"])

    embed = Embed(
        title="✅ COMPRA APROVADA — NEXZY STORE",
        description=f"Obrigado pela sua compra, **{user.display_name}**!\nSeu produto está pronto para uso.",
        color=Color.green()
    )
    embed.add_field(name="📦 Produto", value=produto["nome"], inline=True)
    embed.add_field(name="💰 Valor Pago", value=formatar_preco(produto["preco"]), inline=True)
    embed.add_field(name="🔑 Sua Key de Acesso", value=f"```\n{key}\n```", inline=False)
    embed.add_field(
        name="🔗 Link de Download",
        value=f"[Clique aqui para baixar]({link})\n⚠️ Expira <t:{int(expira_em.timestamp())}:R> ou após o primeiro acesso.",
        inline=False
    )
    embed.add_field(name="📋 Instruções", value="1. Acesse o link acima\n2. Ou use a key diretamente no sistema\n3. Em caso de dúvidas, abra um ticket.", inline=False)
    embed.set_footer(text="NEXZY STORE — Obrigado pela preferência! 💚")
    embed.timestamp = datetime.utcnow()

    try:
        await user.send(embed=embed)
    except Exception as e:
        print(f"Erro ao enviar DM: {e}")

    await log_webhook(
        "💸 Nova Venda Realizada!",
        f"Uma compra foi aprovada e o produto entregue.",
        cor=0x2ecc71,
        campos=[
            {"name": "👤 Comprador", "value": f"<@{user.id}> ({user.name})", "inline": True},
            {"name": "📦 Produto", "value": produto["nome"], "inline": True},
            {"name": "💰 Valor", "value": formatar_preco(produto["preco"]), "inline": True},
            {"name": "🔑 Key", "value": f"`{key}`", "inline": False},
            {"name": "🔗 Token", "value": f"`{token[:20]}...`", "inline": False}
        ]
    )


# ================= PAGAMENTO =================
async def iniciar_pagamento(interaction: discord.Interaction, produto_id):
    produtos = await get_produtos()
    produto = produtos.get(produto_id)
    if not produto:
        return await interaction.followup.send("❌ Produto não encontrado.", ephemeral=True)

    try:
        payment_data = {
            "transaction_amount": float(produto["preco"]),
            "description": f"{produto['nome']} - Nexzy Store",
            "payment_method_id": "pix",
            "payer": {
                "email": f"nexzy_{interaction.user.id}@nexzystore.com.br",
                "first_name": (interaction.user.name or "Cliente")[:50],
                "identification": {"type": "CPF", "number": "00000000000"}
            },
            "additional_info": {
                "items": [{
                    "id": produto_id,
                    "title": produto["nome"],
                    "description": "Compra realizada na Nexzy Store",
                    "category_id": "games",
                    "quantity": 1,
                    "unit_price": float(produto["preco"])
                }]
            },
            "statement_descriptor": "NEXZY STORE"
        }

        payment = sdk.payment().create(payment_data)
        resp = payment["response"]

        pedido_id = str(uuid.uuid4())
        await add_pedido(pedido_id, interaction.user.id, produto_id, produto["nome"], produto["preco"])

        pix_copia_cola = resp["point_of_interaction"]["transaction_data"]["qr_code"]
        payment_id = resp["id"]
        pedidos_pendentes[payment_id] = pedido_id

        embed = Embed(
            title="💳 PAGAMENTO VIA PIX",
            description=f"**{produto['emoji']} {produto['nome']}**\n💰 Valor: **{formatar_preco(produto['preco'])}**",
            color=0x00b300
        )
        embed.add_field(name="🏢 Destinatário", value="**NEXZY STORE**", inline=True)
        embed.add_field(name="⏰ Validade", value="**30 minutos**", inline=True)
        embed.add_field(
            name="📋 Código PIX — Copia e Cola",
            value=f"```\n{pix_copia_cola[:200]}\n```",
            inline=False
        )
        embed.add_field(
            name="📱 Como Pagar",
            value="**1.** Copie o código acima\n**2.** Abra o app do seu banco\n**3.** PIX → Copia e Cola\n**4.** Cole e confirme\n**5.** Clique em ✅ **JÁ PAGUEI**",
            inline=False
        )
        embed.set_footer(text="NEXZY STORE — Pagamento seguro via PIX")
        embed.timestamp = datetime.utcnow()

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="✅ JÁ PAGUEI", style=discord.ButtonStyle.success, custom_id=f"check_{payment_id}"))
        view.add_item(discord.ui.Button(label="❌ CANCELAR", style=discord.ButtonStyle.danger, custom_id=f"cancel_{payment_id}"))

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        asyncio.create_task(verificar_pagamento(payment_id, pedido_id, interaction.user, produto))

        await log_webhook(
            "🕐 Novo Pedido Iniciado",
            "Um usuário iniciou um pagamento.",
            cor=0xf1c40f,
            campos=[
                {"name": "👤 Usuário", "value": f"<@{interaction.user.id}>", "inline": True},
                {"name": "📦 Produto", "value": produto["nome"], "inline": True},
                {"name": "💰 Valor", "value": formatar_preco(produto["preco"]), "inline": True},
            ]
        )

    except Exception as e:
        print(f"Erro PIX: {e}")
        await interaction.followup.send(f"❌ Erro ao gerar PIX: {str(e)[:200]}", ephemeral=True)


async def verificar_pagamento(payment_id, pedido_id, user, produto):
    for _ in range(30):
        await asyncio.sleep(10)
        try:
            info = sdk.payment().get(payment_id)
            if info["response"].get("status") == "approved":
                await update_pedido(pedido_id, "aprovado")
                await add_venda(produto["preco"])
                await entregar_produto(user, produto, pedido_id)
                await atualizar_vendas()
                return
        except Exception as e:
            print(f"Erro verificação: {e}")
    await update_pedido(pedido_id, "expirado")


# ================= LINK DE DOWNLOAD =================
async def handle_download(request):
    """Rota que entrega a key e invalida o link após o acesso"""
    token = request.match_info.get("token")
    if not token:
        return web.Response(text="Token inválido.", status=400)

    try:
        row = await get_key_por_token(token)
        if not row:
            return web.Response(text="Link não encontrado ou inválido.", status=404, content_type="text/html")

        if row["token_usado"]:
            html = """<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#111;color:#fff'>
            <h1>❌ Link Expirado</h1><p>Este link já foi utilizado e não é mais válido.</p>
            <p>Entre em contato com o suporte se precisar de ajuda.</p></body></html>"""
            return web.Response(text=html, content_type="text/html", status=410)

        if datetime.utcnow() > row["token_expira_em"]:
            html = """<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#111;color:#fff'>
            <h1>⏰ Link Expirado</h1><p>Este link expirou após 1 hora.</p>
            <p>Entre em contato com o suporte para obter uma nova chave.</p></body></html>"""
            return web.Response(text=html, content_type="text/html", status=410)

        await marcar_token_usado(token)

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset='utf-8'><title>NEXZY STORE — Download</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background: #0d0d0d; color: #fff; display:flex; justify-content:center; align-items:center; min-height:100vh; margin:0; }}
  .card {{ background: #1a1a2e; border: 1px solid #00ff99; border-radius: 16px; padding: 40px; max-width: 500px; text-align: center; box-shadow: 0 0 30px #00ff9933; }}
  h1 {{ color: #00ff99; }}
  .key {{ background: #0d0d0d; border: 1px solid #00ff99; border-radius: 8px; padding: 16px; font-size: 1.2em; font-family: monospace; color: #00ff99; margin: 20px 0; word-break: break-all; }}
  button {{ background: #00ff99; color: #000; border: none; padding: 12px 28px; border-radius: 8px; font-size: 1em; font-weight: bold; cursor: pointer; }}
  button:hover {{ background: #00cc7a; }}
  .footer {{ margin-top: 20px; color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
  <div class='card'>
    <h1>✅ NEXZY STORE</h1>
    <p>Sua compra foi confirmada! Aqui está sua chave de acesso:</p>
    <div class='key' id='key'>{row['key_valor']}</div>
    <button onclick="navigator.clipboard.writeText('{row['key_valor']}').then(() => this.textContent='✅ Copiado!')">📋 Copiar Key</button>
    <p class='footer'>Produto: <strong>{row['produto_nome']}</strong><br>
    ⚠️ Este link é de uso único e foi invalidado após este acesso.<br>
    Em caso de dúvidas, abra um ticket no servidor.</p>
    <p class='footer'>NEXZY STORE © {datetime.utcnow().year}</p>
  </div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    except Exception as e:
        print(f"Erro download: {e}")
        return web.Response(text="Erro interno.", status=500)


# ================= EMBEDS =================
async def montar_embed_loja():
    produtos = await get_produtos()
    embed = Embed(
        title="🛒  N E X Z Y  S T O R E",
        description="╔══════════════════════════╗\n💎 **Compre via PIX e receba na hora!**\n🔐 Entrega automática | 🔑 Key única por compra\n╚══════════════════════════╝",
        color=0x00ff99
    )
    if produtos:
        for p in produtos.values():
            desc = f"> {p['descricao']}" if p.get("descricao") else ""
            embed.add_field(
                name=f"{p['emoji']}  {p['nome']}",
                value=f"**{formatar_preco(p['preco'])}**\n{desc}",
                inline=True
            )
    else:
        embed.add_field(name="📭 Nenhum produto disponível", value="Em breve novos itens!", inline=False)
    embed.set_footer(text="NEXZY STORE • Clique em 💰 COMPRAR para adquirir")
    embed.timestamp = datetime.utcnow()
    return embed


async def montar_embed_vendas():
    total, qtd = await get_vendas()
    embed = Embed(
        title="📊  ESTATÍSTICAS DE VENDAS",
        description="Painel de desempenho da **NEXZY STORE**",
        color=0xf1c40f
    )
    embed.add_field(name="📦 Total de Vendas", value=f"**{qtd}** pedidos", inline=True)
    embed.add_field(name="💰 Faturamento Total", value=f"**{formatar_preco(total)}**", inline=True)
    embed.add_field(name="📈 Ticket Médio", value=formatar_preco(total / qtd) if qtd > 0 else "R$ 0,00", inline=True)
    embed.set_footer(text="NEXZY STORE — Atualizado agora")
    embed.timestamp = datetime.utcnow()
    return embed


async def atualizar_loja():
    canal = bot.get_channel(CANAL_LOJA)
    if canal:
        async for msg in canal.history(limit=10):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                except Exception:
                    pass
        await canal.send(embed=await montar_embed_loja(), view=LojaButtons())


async def atualizar_vendas():
    canal = bot.get_channel(CANAL_VENDAS)
    if canal:
        async for msg in canal.history(limit=10):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                except Exception:
                    pass
        await canal.send(embed=await montar_embed_vendas())


# ================= WEBHOOK MERCADO PAGO =================
async def webhook_mp(request):
    try:
        data = await request.json()
        if data.get("type") == "payment":
            payment_id = data.get("data", {}).get("id")
            if payment_id and payment_id in pedidos_pendentes:
                info = sdk.payment().get(payment_id)
                if info["response"].get("status") == "approved":
                    pedido_id = pedidos_pendentes[payment_id]
                    async with db.acquire() as conn:
                        pedido = await conn.fetchrow("SELECT * FROM pedidos WHERE id=$1", pedido_id)
                        if pedido and pedido["status"] == "pendente":
                            user = await bot.fetch_user(pedido["user_id"])
                            produtos = await get_produtos()
                            produto = produtos.get(pedido["produto_id"])
                            if produto:
                                await update_pedido(pedido_id, "aprovado")
                                await add_venda(produto["preco"])
                                await entregar_produto(user, produto, pedido_id)
                                await atualizar_vendas()
        return web.Response(status=200)
    except Exception as e:
        print(f"Erro webhook MP: {e}")
        return web.Response(status=200)


async def start_server():
    app = web.Application()
    app.router.add_post("/webhook", webhook_mp)
    app.router.add_get("/download/{token}", handle_download)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    print("✅ Servidor HTTP ativo")


# ================= COMANDOS =================
@bot.command(name="loja")
async def cmd_loja(ctx):
    await ctx.send(embed=await montar_embed_loja(), view=LojaButtons())
    try:
        await ctx.message.delete()
    except Exception:
        pass

@bot.command(name="vendas")
async def cmd_vendas(ctx):
    await ctx.send(embed=await montar_embed_vendas())
    try:
        await ctx.message.delete()
    except Exception:
        pass

@bot.command(name="stats")
async def cmd_stats(ctx):
    if not any(r.id == CARGO_DONO for r in ctx.author.roles):
        return
    total, qtd = await get_vendas()
    await ctx.send(f"📊 **Vendas:** {qtd} | **Total:** {formatar_preco(total)}")

# ================= EVENTOS =================
@bot.event
async def on_ready():
    print(f"✅ Bot online: {bot.user}")
    print("🏢 NEXZY STORE — Sistema Profissional")
    if not await init_db():
        return
    asyncio.create_task(start_server())
    await atualizar_loja()
    await atualizar_vendas()

    await log_webhook(
        "🟢 Bot Online",
        f"**NEXZY STORE** iniciou com sucesso!\nBot: `{bot.user}`",
        cor=0x2ecc71
    )
    print("✅ Bot pronto!")


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")

        if custom_id.startswith("check_"):
            payment_id = int(custom_id.split("_")[1])
            await interaction.response.defer(ephemeral=True)
            try:
                info = sdk.payment().get(payment_id)
                status = info["response"].get("status")
                if status == "approved":
                    await interaction.followup.send("✅ **Pagamento aprovado!** Verifique sua DM com sua key.", ephemeral=True)
                elif status == "pending":
                    await interaction.followup.send("⏳ Pagamento ainda **pendente**. Aguarde alguns instantes.", ephemeral=True)
                else:
                    await interaction.followup.send(f"ℹ️ Status atual: `{status}`", ephemeral=True)
            except Exception:
                await interaction.followup.send("❌ Erro ao verificar. Tente novamente.", ephemeral=True)

        elif custom_id.startswith("cancel_"):
            payment_id = custom_id.split("_")[1]
            await interaction.response.send_message("❌ Pedido cancelado. Você pode iniciar outro quando quiser.", ephemeral=True)

# ================= MAIN =================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
