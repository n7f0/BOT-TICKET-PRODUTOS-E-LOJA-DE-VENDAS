# bot.py - COM ID AUTOMÁTICO
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
from datetime import datetime
from aiohttp import web

# ================= CONFIG =================
CARGO_DONO = int(os.getenv("CARGO_DONO", "0"))
CANAL_LOJA = int(os.getenv("CANAL_LOJA", "0"))
CANAL_VENDAS = int(os.getenv("CANAL_VENDAS", "0"))
DISCORD_TOKEN = os.getenv("LOJA_DISCORD_TOKEN")
MP_TOKEN = os.getenv("MERCADO_PAGO_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

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
    print("✅ URL do banco corrigida")

sdk = mercadopago.SDK(MP_TOKEN)
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

db = None
pedidos_pendentes = {}

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def gerar_id_aleatorio():
    """Gera um ID aleatório de 6 caracteres"""
    caracteres = string.ascii_lowercase + string.digits
    return ''.join(random.choices(caracteres, k=6))

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
                    emoji TEXT DEFAULT '🛒'
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
            
            await conn.execute("INSERT INTO vendas (id, total, quantidade) VALUES (1, 0, 0) ON CONFLICT DO NOTHING")
            
            existing = await conn.fetch("SELECT * FROM produtos")
            if not existing:
                await conn.execute("INSERT INTO produtos VALUES ('prod1', 'VIP Bronze', 19.90, '🥉')")
                await conn.execute("INSERT INTO produtos VALUES ('prod2', 'VIP Prata', 39.90, '🥈')")
                await conn.execute("INSERT INTO produtos VALUES ('prod3', 'VIP Ouro', 69.90, '🥇')")
                print("✅ Produtos exemplo criados")
        
        print("✅ Banco de dados conectado!")
        return True
    except Exception as e:
        print(f"❌ Erro ao conectar ao banco: {e}")
        return False

async def get_produtos():
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM produtos")
        return {r["id"]: dict(r) for r in rows}

async def add_produto(pid, nome, preco, emoji):
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO produtos VALUES ($1,$2,$3,$4)", pid, nome, preco, emoji)

async def remove_produto(pid):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM produtos WHERE id=$1", pid)

async def add_pedido(pid, user_id, produto_id, nome, preco):
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO pedidos VALUES ($1,$2,$3,$4,$5,'pendente',NOW())", pid, user_id, produto_id, nome, preco)

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

# ================= FUNÇÕES =================
def formatar_preco(valor):
    return f"R$ {float(valor):.2f}".replace(".", ",")

def gerar_key():
    return secrets.token_hex(16)

# ================= MODAL COM ID AUTOMÁTICO =================
class ProdutoModal(discord.ui.Modal, title="✨ Adicionar Produto"):
    # Só precisa preencher nome, preço e emoji
    nome_input = discord.ui.TextInput(
        label="📦 Nome do Produto",
        placeholder="Ex: VIP Premium, Cargo Especial",
        required=True,
        max_length=100
    )
    
    preco_input = discord.ui.TextInput(
        label="💰 Preço (R$)",
        placeholder="Ex: 49.90",
        required=True,
        max_length=10
    )
    
    emoji_input = discord.ui.TextInput(
        label="😀 Emoji",
        placeholder="Ex: 👑, 🎁, 💎",
        required=False,
        default="🛒",
        max_length=5
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            # Gerar ID aleatório automaticamente
            produto_id = gerar_id_aleatorio()
            nome = self.nome_input.value
            preco = float(self.preco_input.value.replace(",", "."))
            emoji = self.emoji_input.value or "🛒"
            
            # Verificar se o ID já existe (muito raro, mas possível)
            produtos = await get_produtos()
            while produto_id in produtos:
                produto_id = gerar_id_aleatorio()
            
            await add_produto(produto_id, nome, preco, emoji)
            
            # Mostrar o ID gerado para o admin
            embed = Embed(title="✅ Produto Adicionado!", color=Color.green())
            embed.add_field(name="🆔 ID Gerado", value=f"`{produto_id}`", inline=False)
            embed.add_field(name="📦 Nome", value=nome, inline=True)
            embed.add_field(name="💰 Preço", value=formatar_preco(preco), inline=True)
            embed.add_field(name="😀 Emoji", value=emoji, inline=True)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            await atualizar_loja()
            
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

class RemoverSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = []
        for pid, prod in produtos.items():
            options.append(discord.SelectOption(label=f"{prod['nome']} (ID: {pid})", value=pid, emoji=prod['emoji']))
        super().__init__(placeholder="Selecione o produto para remover", options=options[:25])
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await remove_produto(self.values[0])
        await interaction.followup.send("✅ Produto removido!", ephemeral=True)
        await atualizar_loja()

# ================= VIEWS =================
class ProdutoSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = []
        for pid, prod in produtos.items():
            options.append(discord.SelectOption(label=f"{prod['nome']} - {formatar_preco(prod['preco'])}", value=pid, emoji=prod['emoji']))
        super().__init__(placeholder="Selecione um produto...", options=options[:25])
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await iniciar_pagamento(interaction, self.values[0])

class LojaButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="💰 Comprar", style=discord.ButtonStyle.success)
    async def comprar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await get_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto disponível", ephemeral=True)
        view = discord.ui.View()
        view.add_item(ProdutoSelect(produtos))
        await interaction.response.send_message("📦 Escolha seu produto:", view=view, ephemeral=True)
    
    @discord.ui.button(label="👑 Admin", style=discord.ButtonStyle.danger)
    async def admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == CARGO_DONO for r in interaction.user.roles):
            return await interaction.response.send_message("❌ Sem permissão", ephemeral=True)
        
        view = discord.ui.View(timeout=60)
        view.add_item(discord.ui.Button(label="➕ Adicionar Produto", style=discord.ButtonStyle.success, custom_id="add_prod"))
        view.add_item(discord.ui.Button(label="🗑️ Remover Produto", style=discord.ButtonStyle.danger, custom_id="remove_prod"))
        await interaction.response.send_message("👑 **Painel Admin**\nO ID do produto será gerado automaticamente!", view=view, ephemeral=True)

async def montar_embed_loja():
    produtos = await get_produtos()
    embed = Embed(title="🛒 NEXZY STORE", description="💎 Compre via PIX e receba na hora", color=Color.blue())
    embed.set_image(url="https://media.discordapp.net/attachments/1491808878562643998/1491808965170958396/e6876514-c5ae-477f-a84b-d7b7db0c01e5.png")
    
    for p in produtos.values():
        embed.add_field(name=f"{p['emoji']} {p['nome']}", value=f"{formatar_preco(p['preco'])}", inline=True)
    
    embed.set_footer(text="Clique em COMPRAR para adquirir")
    return embed

async def montar_embed_vendas():
    total, qtd = await get_vendas()
    embed = Embed(title="📊 ESTATÍSTICAS", color=Color.gold())
    embed.add_field(name="📦 Vendas", value=str(qtd), inline=True)
    embed.add_field(name="💰 Faturamento", value=formatar_preco(total), inline=True)
    return embed

async def atualizar_loja():
    canal = bot.get_channel(CANAL_LOJA)
    if canal:
        async for msg in canal.history(limit=10):
            if msg.author == bot.user:
                await msg.delete()
        embed = await montar_embed_loja()
        await canal.send(embed=embed, view=LojaButtons())

async def atualizar_vendas():
    canal = bot.get_channel(CANAL_VENDAS)
    if canal:
        async for msg in canal.history(limit=10):
            if msg.author == bot.user:
                await msg.delete()
        embed = await montar_embed_vendas()
        await canal.send(embed=embed)

# ================= PAGAMENTO =================
async def iniciar_pagamento(interaction: discord.Interaction, produto_id):
    produtos = await get_produtos()
    produto = produtos.get(produto_id)
    if not produto:
        return await interaction.followup.send("❌ Produto não encontrado", ephemeral=True)
    
    try:
        payment = sdk.payment().create({
            "transaction_amount": float(produto["preco"]),
            "description": produto["nome"],
            "payment_method_id": "pix",
            "payer": {"email": f"user{interaction.user.id}@email.com"}
        })
        
        resp = payment["response"]
        pedido_id = str(uuid.uuid4())
        
        await add_pedido(pedido_id, interaction.user.id, produto_id, produto["nome"], produto["preco"])
        
        qr_code = resp["point_of_interaction"]["transaction_data"]["qr_code"]
        qr_text = resp["point_of_interaction"]["transaction_data"]["qr_code_base64"]
        payment_id = resp["id"]
        
        pedidos_pendentes[payment_id] = pedido_id
        
        embed = Embed(title="💳 PIX", description=f"**{produto['nome']}**\n{formatar_preco(produto['preco'])}", color=Color.green())
        embed.add_field(name="📱 Código PIX", value=f"```{qr_text}```", inline=False)
        embed.set_image(url=qr_code)
        embed.set_footer(text="Após pagar, clique em JÁ PAGUEI")
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="✅ Já paguei", style=discord.ButtonStyle.success, custom_id=f"check_{payment_id}"))
        view.add_item(discord.ui.Button(label="❌ Cancelar", style=discord.ButtonStyle.danger, custom_id=f"cancel_{payment_id}"))
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        
        asyncio.create_task(verificar_pagamento(payment_id, pedido_id, interaction.user, produto))
        
    except Exception as e:
        await interaction.followup.send(f"❌ Erro ao gerar PIX: {e}", ephemeral=True)

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
        except:
            pass
    
    await update_pedido(pedido_id, "expirado")

async def entregar_produto(user, produto, pedido_id):
    key = secrets.token_hex(16)
    
    embed = Embed(title="✅ COMPRA APROVADA!", description=f"{produto['nome']} - {formatar_preco(produto['preco'])}", color=Color.green())
    embed.add_field(name="🔑 SUA KEY", value=f"```{key}```", inline=False)
    embed.add_field(name="📁 PRODUTO ENTREGUE", value="Aproveite seu produto!", inline=False)
    embed.set_footer(text="Obrigado pela compra!")
    
    try:
        await user.send(embed=embed)
    except:
        pass

# ================= COMANDOS =================
@bot.command(name="loja")
async def cmd_loja(ctx):
    embed = await montar_embed_loja()
    await ctx.send(embed=embed, view=LojaButtons())
    await ctx.message.delete()

@bot.command(name="vendas")
async def cmd_vendas(ctx):
    embed = await montar_embed_vendas()
    await ctx.send(embed=embed)
    await ctx.message.delete()

# ================= WEBHOOK =================
async def webhook(request):
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
    except:
        return web.Response(status=200)

async def start_webhook():
    app = web.Application()
    app.router.add_post("/webhook", webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    print("✅ Webhook ativo")

# ================= EVENTOS =================
@bot.event
async def on_ready():
    print(f"✅ Bot logado como {bot.user}")
    
    if not await init_db():
        print("❌ Falha no banco de dados!")
        return
    
    print(f"🛒 Canal Loja: {CANAL_LOJA}")
    print(f"📊 Canal Vendas: {CANAL_VENDAS}")
    
    asyncio.create_task(start_webhook())
    
    await atualizar_loja()
    await atualizar_vendas()
    
    print("✅ Bot pronto! Use !loja")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")
        
        if custom_id == "add_prod":
            await interaction.response.send_modal(ProdutoModal())
        
        elif custom_id == "remove_prod":
            produtos = await get_produtos()
            if not produtos:
                return await interaction.response.send_message("❌ Nenhum produto cadastrado", ephemeral=True)
            view = discord.ui.View()
            view.add_item(RemoverSelect(produtos))
            await interaction.response.send_message("🗑️ Selecione o produto para remover:", view=view, ephemeral=True)
        
        elif custom_id.startswith("check_"):
            payment_id = int(custom_id.split("_")[1])
            await interaction.response.send_message("⏳ Verificando pagamento...", ephemeral=True)
            try:
                info = sdk.payment().get(payment_id)
                if info["response"].get("status") == "approved":
                    await interaction.edit_original_response(content="✅ Pagamento aprovado! Verifique sua DM.", embed=None, view=None)
                else:
                    await interaction.edit_original_response(content="⏳ Aguardando pagamento...", embed=None, view=None)
            except:
                await interaction.edit_original_response(content="❌ Erro ao verificar", embed=None, view=None)
        
        elif custom_id.startswith("cancel_"):
            await interaction.response.send_message("❌ Pedido cancelado", ephemeral=True)

# ================= MAIN =================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)