# bot.py - NEXZY STORE - FINAL CORRIGIDO (titulo)
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
import subprocess
import tempfile
import shutil
import io
from datetime import datetime, timedelta
from aiohttp import web

# ================= CONFIG =================
CARGO_DONO      = int(os.getenv("CARGO_DONO", "0"))
CANAL_LOJA      = int(os.getenv("CANAL_LOJA", "0"))
CANAL_VENDAS    = int(os.getenv("CANAL_VENDAS", "0"))
CANAL_LOG_VENDAS = int(os.getenv("CANAL_LOG_VENDAS", "1492726744514428980"))
CANAL_LOG_ADMIN  = int(os.getenv("CANAL_LOG_ADMIN", "1502680283470758041"))
GUILD_ID         = int(os.getenv("GUILD_ID", "0"))
DISCORD_TOKEN   = os.getenv("LOJA_DISCORD_TOKEN")
MP_TOKEN        = os.getenv("MERCADO_PAGO_TOKEN")
DATABASE_URL    = os.getenv("DATABASE_URL")

# CORES TEMA PRETO
COR_PRINCIPAL   = 0x1a1a1a
COR_SUCESSO     = 0x2d2d2d
COR_ERRO        = 0x8b0000
COR_PENDENTE    = 0x3d3d3d
COR_DESTAQUE    = 0x4a4a4a

for var, nome in [(DISCORD_TOKEN,"LOJA_DISCORD_TOKEN"),(MP_TOKEN,"MERCADO_PAGO_TOKEN"),(DATABASE_URL,"DATABASE_URL")]:
    if not var:
        print(f"❌ {nome} não configurado!")
        exit(1)

if "railwaypostgresql://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("railwaypostgresql://", "postgresql://")

sdk     = mercadopago.SDK(MP_TOKEN)
intents = discord.Intents.all()
bot     = commands.Bot(command_prefix="!", intents=intents)

db                = None
pedidos_pendentes = {}

# ================= HELPERS =================
def gerar_id():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

def gerar_senha_arquivo():
    return ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=32))

def formatar_preco(v):
    return f"R$ {float(v):.2f}".replace(".", ",")

def verificar_7zip():
    return shutil.which("7z") is not None

def criar_embed(titulo="", descricao="", cor=COR_PRINCIPAL):
    embed = Embed(title=titulo, description=descricao, color=cor)
    embed.set_footer(text="⚫ NEXZY STORE")
    embed.timestamp = datetime.utcnow()
    return embed

async def get_guild():
    return bot.get_guild(GUILD_ID)

# ================= BANCO =================
async def init_db():
    global db
    try:
        db = await asyncpg.create_pool(DATABASE_URL)
        async with db.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS produtos (
                    id           TEXT PRIMARY KEY,
                    nome         TEXT NOT NULL,
                    preco        REAL NOT NULL,
                    emoji        TEXT DEFAULT '🛒',
                    descricao    TEXT DEFAULT '',
                    arquivo_nome TEXT DEFAULT NULL,
                    arquivo_data BYTEA DEFAULT NULL
                )
            """)
            for col, tipo in [
                ("descricao","TEXT DEFAULT ''"),
                ("arquivo_nome","TEXT DEFAULT NULL"),
                ("arquivo_data","BYTEA DEFAULT NULL")
            ]:
                try:
                    await conn.execute(f"ALTER TABLE produtos ADD COLUMN IF NOT EXISTS {col} {tipo}")
                except Exception:
                    pass

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pedidos (
                    id            TEXT PRIMARY KEY,
                    user_id       BIGINT NOT NULL,
                    produto_id    TEXT NOT NULL,
                    produto_nome  TEXT NOT NULL,
                    produto_preco REAL NOT NULL,
                    status        TEXT DEFAULT 'pendente',
                    criado_em     TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vendas (
                    id         SERIAL PRIMARY KEY,
                    total      REAL DEFAULT 0,
                    quantidade INTEGER DEFAULT 0
                )
            """)
            await conn.execute("INSERT INTO vendas (id,total,quantidade) VALUES (1,0,0) ON CONFLICT (id) DO NOTHING")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vendas_realizadas (
                    id           SERIAL PRIMARY KEY,
                    pedido_id    TEXT NOT NULL,
                    user_id      BIGINT NOT NULL,
                    produto_nome TEXT NOT NULL,
                    valor        REAL NOT NULL,
                    criado_em    TIMESTAMP DEFAULT NOW()
                )
            """)
            if not await conn.fetch("SELECT id FROM produtos LIMIT 1"):
                await conn.execute("INSERT INTO produtos (id,nome,preco,emoji,descricao) VALUES ('prod1','VIP Bronze',19.90,'🥉','Acesso VIP Bronze')")
                await conn.execute("INSERT INTO produtos (id,nome,preco,emoji,descricao) VALUES ('prod2','VIP Prata',39.90,'🥈','Acesso VIP Prata')")
                await conn.execute("INSERT INTO produtos (id,nome,preco,emoji,descricao) VALUES ('prod3','VIP Ouro',69.90,'🥇','Acesso VIP Ouro')")
        print("✅ Banco conectado!")
        return True
    except Exception as e:
        print(f"❌ Erro banco: {e}")
        return False

async def get_produtos():
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT id,nome,preco,emoji,descricao,arquivo_nome FROM produtos")
        return {r["id"]: dict(r) for r in rows}

async def get_produto_completo(pid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM produtos WHERE id=$1", pid)

async def add_produto(pid, nome, preco, emoji, descricao=""):
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO produtos (id,nome,preco,emoji,descricao) VALUES ($1,$2,$3,$4,$5)", pid, nome, preco, emoji, descricao)

async def edit_produto(pid, nome, preco, emoji, descricao):
    async with db.acquire() as conn:
        await conn.execute("UPDATE produtos SET nome=$2,preco=$3,emoji=$4,descricao=$5 WHERE id=$1", pid, nome, preco, emoji, descricao)

async def salvar_arquivo_produto(pid, nome_arquivo, dados: bytes):
    async with db.acquire() as conn:
        await conn.execute("UPDATE produtos SET arquivo_nome=$2, arquivo_data=$3 WHERE id=$1", pid, nome_arquivo, dados)

async def remover_arquivo_produto(pid):
    async with db.acquire() as conn:
        await conn.execute("UPDATE produtos SET arquivo_nome=NULL, arquivo_data=NULL WHERE id=$1", pid)

async def remove_produto(pid):
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM produtos WHERE id=$1", pid)

async def add_pedido(pid, user_id, produto_id, nome, preco):
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO pedidos (id,user_id,produto_id,produto_nome,produto_preco) VALUES ($1,$2,$3,$4,$5)", pid, user_id, produto_id, nome, preco)

async def update_pedido(pid, status):
    async with db.acquire() as conn:
        await conn.execute("UPDATE pedidos SET status=$1 WHERE id=$2", status, pid)

async def get_vendas():
    async with db.acquire() as conn:
        r = await conn.fetchrow("SELECT total,quantidade FROM vendas WHERE id=1")
        return r["total"], r["quantidade"]

async def add_venda(valor):
    async with db.acquire() as conn:
        await conn.execute("UPDATE vendas SET total=total+$1, quantidade=quantidade+1 WHERE id=1", valor)

async def registrar_venda_realizada(pedido_id, user_id, produto_nome, valor):
    async with db.acquire() as conn:
        await conn.execute("INSERT INTO vendas_realizadas (pedido_id,user_id,produto_nome,valor) VALUES ($1,$2,$3,$4)", pedido_id, user_id, produto_nome, valor)

async def limpar_banco_completo():
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM vendas_realizadas")
        await conn.execute("DELETE FROM pedidos")
        await conn.execute("DELETE FROM produtos")
        await conn.execute("UPDATE vendas SET total=0, quantidade=0 WHERE id=1")

# ================= LOGS =================
async def log_venda(pedido_id, user, produto, valor, senha_arquivo=None):
    canal = bot.get_channel(CANAL_LOG_VENDAS)
    if not canal: return
    embed = criar_embed(titulo="🖤 VENDA FINALIZADA", descricao="Nova compra aprovada com sucesso!", cor=COR_SUCESSO)
    embed.add_field(name="🆔 Pedido", value=f"`{pedido_id}`", inline=True)
    embed.add_field(name="👤 Comprador", value=f"<@{user.id}> ({user.name})", inline=True)
    embed.add_field(name="📦 Produto", value=produto, inline=True)
    embed.add_field(name="💰 Valor", value=formatar_preco(valor), inline=True)
    embed.add_field(name="🔐 Senha", value=f"`{senha_arquivo}`" if senha_arquivo else "Sem arquivo", inline=False)
    await canal.send(embed=embed)

async def log_admin(acao, admin, detalhes, cor=COR_DESTAQUE):
    canal = bot.get_channel(CANAL_LOG_ADMIN)
    if not canal: return
    embed = criar_embed(titulo=f"⚙️ ADMIN • {acao}", descricao=detalhes, cor=cor)
    embed.add_field(name="👑 Admin", value=f"<@{admin.id}> ({admin.name})", inline=True)
    await canal.send(embed=embed)

# ================= CRIPTOGRAFIA 7ZIP =================
def _criar_7z_sync(dados: bytes, nome_original: str, senha: str) -> bytes:
    tmp = tempfile.mkdtemp(prefix="nexzy_")
    try:
        caminho_original = os.path.join(tmp, nome_original)
        with open(caminho_original, "wb") as f:
            f.write(dados)
        caminho_saida = os.path.join(tmp, "entrega.7z")
        resultado = subprocess.run(
            ["7z", "a", f"-p{senha}", "-mhe=on", "-mx=0", caminho_saida, caminho_original],
            capture_output=True, text=True, timeout=120
        )
        if resultado.returncode != 0:
            raise RuntimeError(f"7zip error: {resultado.stderr.strip()}")
        with open(caminho_saida, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

async def criar_7z_criptografado(dados: bytes, nome_original: str, senha: str) -> bytes:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _criar_7z_sync, dados, nome_original, senha)

# ================= ENTREGA VIA CANAL TEMPORÁRIO (CORRIGIDA) =================
async def entregar_produto(user, produto: dict, pedido_id: str, guild, dados_arquivo_override: bytes = None, nome_arquivo_override: str = None):
    
    senha_arquivo = None
    tem_arquivo = False
    dados_raw = None
    nome_original = None

    if dados_arquivo_override is not None:
        tem_arquivo = True
        senha_arquivo = gerar_senha_arquivo()
        dados_raw = dados_arquivo_override
        nome_original = nome_arquivo_override or "arquivo_nexzy"
    else:
        prod_completo = await get_produto_completo(produto["id"])
        if prod_completo is not None:
            arquivo_data = prod_completo["arquivo_data"]
            arquivo_nome = prod_completo["arquivo_nome"]
            if arquivo_data is not None:
                tem_arquivo = True
                senha_arquivo = gerar_senha_arquivo()
                if isinstance(arquivo_data, memoryview):
                    dados_raw = arquivo_data.tobytes()
                elif isinstance(arquivo_data, (bytes, bytearray)):
                    dados_raw = bytes(arquivo_data)
                else:
                    dados_raw = bytes(arquivo_data)
                nome_original = arquivo_nome or f"produto_{produto['id']}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    cargo_dono = guild.get_role(CARGO_DONO)
    if cargo_dono:
        overwrites[cargo_dono] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    nome_canal = f"🛒-compra-{user.name.lower().replace(' ', '-')[:20]}"
    try:
        canal_temp = await guild.create_text_channel(
            name=nome_canal,
            overwrites=overwrites,
            reason=f"Entrega para {user.name} • pedido {pedido_id}"
        )
    except discord.Forbidden:
        try:
            embed = criar_embed(titulo="⚠️ Erro na entrega", descricao="Não foi possível criar o canal.", cor=COR_ERRO)
            await user.send(embed=embed)
        except:
            pass
        return

    embed = discord.Embed(
        title="🖤  NEXZY STORE — COMPRA APROVADA",
        description=(
            f"> Olá, **{user.display_name}**! Seu pagamento foi **confirmado**.\n"
            f"> Este canal será excluído em **30 minutos**."
        ),
        color=0x2b2b2b
    )
    embed.set_thumbnail(url=user.display_avatar.url if user.display_avatar else None)
    embed.add_field(name="**━━━━━━━━━━━━━━━━━━━━**", value="\u200b", inline=False)
    embed.add_field(name="**📦  Produto**",   value=f"{produto['emoji']}  {produto['nome']}", inline=True)
    embed.add_field(name="**💳  Valor Pago**", value=f"`{formatar_preco(produto['preco'])}`", inline=True)
    embed.add_field(name="**🆔  Pedido**",     value=f"`{pedido_id}`", inline=True)
    embed.add_field(name="**━━━━━━━━━━━━━━━━━━━━**", value="\u200b", inline=False)

    if tem_arquivo:
        embed.add_field(name="**🔐  Senha do Arquivo `.7z`**", value=f"```\n{senha_arquivo}\n```", inline=False)
        embed.add_field(
            name="**📂  Como extrair**",
            value=(
                "**1.** Baixe o arquivo `.7z` abaixo\n"
                "**2.** Instale o **[7-Zip](https://7-zip.org)** (gratuito)\n"
                "**3.** Clique com botão direito → *7-Zip → Extrair aqui*\n"
                "**4.** Insira a senha acima\n\n"
                "⚠️ **Arquivo exclusivo para esta compra.**"
            ),
            inline=False
        )
        embed.set_footer(text="⚫ NEXZY STORE  •  Suporte disponível via ticket")
        embed.timestamp = datetime.utcnow()

        try:
            if not verificar_7zip():
                raise RuntimeError("7-Zip não encontrado. Execute: apt install p7zip-full")
            dados_cifrados = await criar_7z_criptografado(dados_raw, nome_original, senha_arquivo)
            nome_saida = f"nexzy_{produto['id']}_{pedido_id[:8]}.7z"
            arquivo_discord = discord.File(fp=io.BytesIO(dados_cifrados), filename=nome_saida)
            await canal_temp.send(embed=embed, file=arquivo_discord)
            print(f"✅ Arquivo entregue: {nome_saida}")
        except Exception as e:
            print(f"❌ Erro criptografia: {e}")
            embed.add_field(name="⚠️  Problema na entrega", value="Houve um erro. O suporte foi notificado.", inline=False)
            await canal_temp.send(embed=embed)
            canal_admin = bot.get_channel(CANAL_LOG_ADMIN)
            if canal_admin:
                await canal_admin.send(embed=criar_embed(
                    titulo="❌ ERRO NA ENTREGA",
                    descricao=f"Pedido `{pedido_id}`\nUsuário {user.mention}\nProduto {produto['nome']}\nErro: `{e}`",
                    cor=COR_ERRO
                ))
    else:
        embed.add_field(name="**✅  Próximos passos**", value="Produto ativado. Abra um ticket se precisar.", inline=False)
        embed.set_footer(text="⚫ NEXZY STORE  •  Suporte disponível via ticket")
        embed.timestamp = datetime.utcnow()
        await canal_temp.send(embed=embed)

    async def remover_canal():
        await asyncio.sleep(1800)
        try:
            await canal_temp.delete(reason="Canal de entrega expirado")
        except:
            pass
    asyncio.create_task(remover_canal())

    if not pedido_id.startswith("TESTE-"):
        await registrar_venda_realizada(pedido_id, user.id, produto["nome"], produto["preco"])
        await log_venda(pedido_id, user, produto["nome"], produto["preco"], senha_arquivo)
    else:
        await log_admin("Teste de Entrega", user, f"Pedido `{pedido_id}` | Produto: {produto['nome']}")

# ================= MODALS =================
class ProdutoModal(discord.ui.Modal, title="✨ Adicionar Produto"):
    nome_input      = discord.ui.TextInput(label="📦 Nome", placeholder="Ex: VIP Premium", required=True)
    preco_input     = discord.ui.TextInput(label="💰 Preço", placeholder="49.90", required=True)
    emoji_input     = discord.ui.TextInput(label="😀 Emoji", placeholder="👑", required=False, default="🛒")
    descricao_input = discord.ui.TextInput(label="📝 Descrição", placeholder="Breve descrição", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            pid = gerar_id()
            nome = self.nome_input.value
            preco = float(self.preco_input.value.replace(",", "."))
            emoji = self.emoji_input.value or "🛒"
            descricao = self.descricao_input.value or ""
            produtos = await get_produtos()
            while pid in produtos: pid = gerar_id()
            await add_produto(pid, nome, preco, emoji, descricao)
            embed = criar_embed(titulo="✅ Produto Adicionado!", cor=COR_SUCESSO)
            embed.add_field(name="🆔 ID", value=f"`{pid}`", inline=True)
            embed.add_field(name="📦 Nome", value=nome, inline=True)
            embed.add_field(name="💰 Preço", value=formatar_preco(preco), inline=True)
            embed.add_field(name="📂 Vincular arquivo", value=f"`!upload {pid}`", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            await atualizar_loja()
            await log_admin("Produto Adicionado", interaction.user, f"**{nome}** • {formatar_preco(preco)} • ID `{pid}`")
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

class EditarProdutoModal(discord.ui.Modal, title="✏️ Editar Produto"):
    def __init__(self, produto):
        super().__init__()
        self.produto_id = produto["id"]
        self.nome_input = discord.ui.TextInput(label="📦 Nome", default=produto["nome"], required=True)
        self.preco_input = discord.ui.TextInput(label="💰 Preço", default=str(produto["preco"]), required=True)
        self.emoji_input = discord.ui.TextInput(label="😀 Emoji", default=produto["emoji"], required=False)
        self.descricao_input = discord.ui.TextInput(label="📝 Descrição", default=produto.get("descricao",""), required=False)
        for item in [self.nome_input, self.preco_input, self.emoji_input, self.descricao_input]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            nome = self.nome_input.value
            preco = float(self.preco_input.value.replace(",", "."))
            emoji = self.emoji_input.value or "🛒"
            descricao = self.descricao_input.value or ""
            await edit_produto(self.produto_id, nome, preco, emoji, descricao)
            await interaction.followup.send("✅ Produto editado!", ephemeral=True)
            await atualizar_loja()
            await log_admin("Produto Editado", interaction.user, f"**{nome}** • {formatar_preco(preco)} • ID `{self.produto_id}`")
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

# ================= SELECTS =================
class RemoverSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = [discord.SelectOption(label=f"{p['nome']} ({pid})", value=pid, emoji=p['emoji']) for pid, p in produtos.items()]
        super().__init__(placeholder="🗑️ Selecione o produto para remover", options=options[:25])
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        produtos = await get_produtos()
        produto = produtos.get(self.values[0])
        nome = produto["nome"] if produto else self.values[0]
        await remove_produto(self.values[0])
        await interaction.followup.send(f"✅ **{nome}** removido!", ephemeral=True)
        await atualizar_loja()
        await log_admin("Produto Removido", interaction.user, f"**{nome}** • ID `{self.values[0]}`", cor=COR_ERRO)

class EditarSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = [discord.SelectOption(label=f"{p['nome']} — {formatar_preco(p['preco'])}", value=pid, emoji=p['emoji']) for pid, p in produtos.items()]
        super().__init__(placeholder="✏️ Selecione o produto para editar", options=options[:25])
    async def callback(self, interaction: discord.Interaction):
        produtos = await get_produtos()
        produto = produtos.get(self.values[0])
        if not produto: return await interaction.response.send_message("❌ Produto não encontrado.", ephemeral=True)
        await interaction.response.send_modal(EditarProdutoModal(produto))

class ProdutoSelect(discord.ui.Select):
    def __init__(self, produtos):
        options = [discord.SelectOption(label=f"{p['nome']} — {formatar_preco(p['preco'])}", value=pid, emoji=p['emoji']) for pid, p in produtos.items()]
        super().__init__(placeholder="🛒 Escolha um produto", options=options[:25])
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await iniciar_pagamento(interaction, self.values[0])

# ================= VIEWS =================
class ConfirmacaoLimpezaView(discord.ui.View):
    def __init__(self, interaction_original):
        super().__init__(timeout=60)
        self.interaction_original = interaction_original

    async def on_timeout(self):
        try:
            await self.interaction_original.edit_original_response(content="⏰ Tempo expirado.", embed=None, view=None)
        except:
            pass

    @discord.ui.button(label="✅ CONFIRMAR LIMPEZA", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_original.user.id:
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await limpar_banco_completo()
        await atualizar_loja()
        await atualizar_vendas()
        await log_admin("🗑️ Banco Limpo", interaction.user, "Todos os dados foram zerados.", cor=COR_ERRO)
        embed = criar_embed(titulo="✅ Banco de Dados Limpo", descricao="Tudo foi removido.", cor=COR_SUCESSO)
        await self.interaction_original.edit_original_response(embed=embed, view=None)
        await interaction.followup.send("✅ Limpo!", ephemeral=True)

    @discord.ui.button(label="❌ CANCELAR", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.interaction_original.user.id:
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        embed = criar_embed(titulo="❌ Cancelado", descricao="Banco intacto.", cor=COR_DESTAQUE)
        await self.interaction_original.edit_original_response(embed=embed, view=None)
        await interaction.followup.send("✅ Cancelado.", ephemeral=True)

class LojaButtons(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="💰 Comprar", style=discord.ButtonStyle.success, emoji="🛒")
    async def comprar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await get_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto disponível.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(ProdutoSelect(produtos))
        await interaction.response.send_message("📦 **Selecione o produto:**", view=view, ephemeral=True)

    @discord.ui.button(label="👑 Admin", style=discord.ButtonStyle.danger, emoji="⚙️")
    async def admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == CARGO_DONO for r in interaction.user.roles):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        embed = criar_embed(titulo="⚙️ Painel Admin", cor=COR_ERRO)
        embed.add_field(name="➕ Adicionar", value="Cadastra produto", inline=True)
        embed.add_field(name="✏️ Editar", value="Altera produto", inline=True)
        embed.add_field(name="🗑️ Remover", value="Remove produto", inline=True)
        embed.add_field(name="📂 Ver Arquivos", value="Arquivos do banco", inline=True)
        embed.add_field(name="🧹 Limpar Banco", value="Limpa tudo", inline=True)
        embed.add_field(name="🧪 Teste de Entrega", value="Envia `teste_nexzy.txt`", inline=True)
        embed.add_field(name="📊 Estatísticas", value="Faturamento", inline=True)
        embed.add_field(name="📂 Upload", value="`!upload <id>`", inline=True)
        await interaction.response.send_message(embed=embed, view=AdminView(), ephemeral=True)

class AdminView(discord.ui.View):
    def __init__(self): super().__init__(timeout=120)
    @discord.ui.button(label="➕ Adicionar", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ProdutoModal())

    @discord.ui.button(label="✏️ Editar", style=discord.ButtonStyle.primary)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await get_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(EditarSelect(produtos))
        await interaction.response.send_message("✏️ Selecione o produto:", view=view, ephemeral=True)

    @discord.ui.button(label="🗑️ Remover", style=discord.ButtonStyle.danger)
    async def remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await get_produtos()
        if not produtos:
            return await interaction.response.send_message("❌ Nenhum produto.", ephemeral=True)
        view = discord.ui.View()
        view.add_item(RemoverSelect(produtos))
        await interaction.response.send_message("🗑️ Selecione o produto:", view=view, ephemeral=True)

    @discord.ui.button(label="📂 Ver Arquivos", style=discord.ButtonStyle.secondary)
    async def ver_arquivos(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT id, nome, arquivo_nome, LENGTH(arquivo_data) as tamanho_bytes FROM produtos WHERE arquivo_data IS NOT NULL")
        if not rows:
            embed = criar_embed(titulo="📂 Arquivos", descricao="*Nenhum.*", cor=COR_DESTAQUE)
            return await interaction.followup.send(embed=embed, ephemeral=True)
        embed = criar_embed(titulo="📂 Arquivos no Banco", descricao=f"{len(rows)} arquivo(s):", cor=COR_DESTAQUE)
        total = 0
        for row in rows:
            mb = row["tamanho_bytes"]/1024/1024
            total += row["tamanho_bytes"]
            embed.add_field(name=f"📦 {row['nome']} (`{row['id']}`)", value=f"📄 `{row['arquivo_nome']}`\n📏 {mb:.2f} MB", inline=False)
        embed.add_field(name="📊 Total", value=f"**{len(rows)}** arquivos • **{total/1024/1024:.2f} MB**", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🧹 Limpar Banco", style=discord.ButtonStyle.danger)
    async def limpar_banco(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = criar_embed(titulo="⚠️ CONFIRMAÇÃO", descricao="**IRREVERSÍVEL!** Apagará tudo.", cor=COR_ERRO)
        view = ConfirmacaoLimpezaView(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🧪 Teste de Entrega", style=discord.ButtonStyle.secondary)
    async def teste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild or await get_guild()
        if not guild:
            return await interaction.followup.send("❌ Servidor não encontrado.", ephemeral=True)
        conteudo = b"Arquivo de teste da Nexzy Store.\nSe voce esta vendo isso, a entrega funcionou!"
        produto_teste = {"id":"teste","nome":"Produto de Teste","preco":0.0,"emoji":"🧪"}
        pedido_id = f"TESTE-{uuid.uuid4().hex[:8]}"
        await interaction.followup.send("⏳ Criando canal de teste...", ephemeral=True)
        await entregar_produto(interaction.user, produto_teste, pedido_id, guild, dados_arquivo_override=conteudo, nome_arquivo_override="teste_nexzy.txt")
        await interaction.edit_original_response(content="✅ Canal de teste criado! Verifique o servidor.")

    @discord.ui.button(label="📊 Estatísticas", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await montar_embed_vendas(), ephemeral=True)

# ================= PAGAMENTO =================
async def iniciar_pagamento(interaction: discord.Interaction, produto_id: str):
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
            "statement_descriptor": "NEXZY STORE"
        }
        payment = sdk.payment().create(payment_data)
        resp = payment["response"]
        pedido_id = str(uuid.uuid4())
        await add_pedido(pedido_id, interaction.user.id, produto_id, produto["nome"], produto["preco"])
        pix = resp["point_of_interaction"]["transaction_data"]["qr_code"]
        pay_id = resp["id"]
        pedidos_pendentes[pay_id] = pedido_id
        embed = criar_embed(titulo="💳 PAGAMENTO VIA PIX",
                            descricao=f"**{produto['emoji']} {produto['nome']}**\n💰 **{formatar_preco(produto['preco'])}**",
                            cor=COR_PENDENTE)
        embed.add_field(name="🏢 Destinatário", value="**NEXZY STORE**", inline=True)
        embed.add_field(name="⏰ Validade", value="**30 minutos**", inline=True)
        embed.add_field(name="📋 Código PIX", value=f"```\n{pix[:300]}\n```", inline=False)
        embed.add_field(name="📱 Como Pagar", value="1. Copie o código\n2. PIX no seu banco\n3. Clique em ✅ JÁ PAGUEI", inline=False)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="✅ JÁ PAGUEI", style=discord.ButtonStyle.success, custom_id=f"check_{pay_id}"))
        view.add_item(discord.ui.Button(label="❌ CANCELAR", style=discord.ButtonStyle.danger, custom_id=f"cancel_{pay_id}"))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        guild = interaction.guild or await get_guild()
        asyncio.create_task(verificar_pagamento(pay_id, pedido_id, interaction.user, produto, guild))
    except Exception as e:
        print(f"Erro PIX: {e}")
        await interaction.followup.send(f"❌ Erro: {str(e)[:200]}", ephemeral=True)

async def verificar_pagamento(payment_id, pedido_id, user, produto, guild):
    for _ in range(30):
        await asyncio.sleep(10)
        try:
            info = sdk.payment().get(payment_id)
            if info["response"].get("status") == "approved":
                await update_pedido(pedido_id, "aprovado")
                await add_venda(produto["preco"])
                await entregar_produto(user, dict(produto), pedido_id, guild)
                await atualizar_vendas()
                return
        except Exception as e:
            print(f"Verificação: {e}")
    await update_pedido(pedido_id, "expirado")

# ================= EMBEDS LOJA / VENDAS =================
async def montar_embed_loja():
    produtos = await get_produtos()
    embed = criar_embed(titulo="**🖤  N E X Z Y  S T O R E**",
                        descricao="╔══════════════════════════╗\n💎 **Compre via PIX e receba em canal exclusivo!**\n🔐 Arquivo criptografado + senha única\n╚══════════════════════════╝",
                        cor=0x1a1a1a)
    for pid, p in produtos.items():
        desc = p.get("descricao") or ""
        arquivo = "📂 Arquivo incluído" if p.get("arquivo_nome") else "🔑 Acesso imediato"
        embed.add_field(name=f"{p['emoji']}  {p['nome']}",
                        value=f"**{formatar_preco(p['preco'])}**\n🆔 `{pid}`\n{arquivo}" + (f"\n> {desc}" if desc else ""),
                        inline=True)
    embed.set_footer(text="⚫ NEXZY STORE • Clique em 💰 COMPRAR")
    embed.timestamp = datetime.utcnow()
    return embed

async def montar_embed_vendas():
    total, qtd = await get_vendas()
    embed = criar_embed(titulo="📊 ESTATÍSTICAS — NEXZY STORE", cor=COR_DESTAQUE)
    embed.add_field(name="📦 Vendas", value=f"**{qtd}** pedidos", inline=True)
    embed.add_field(name="💰 Faturamento", value=f"**{formatar_preco(total)}**", inline=True)
    embed.add_field(name="📈 Ticket Médio", value=formatar_preco(total/qtd) if qtd else "R$ 0,00", inline=True)
    return embed

# ================= ATUALIZAÇÕES (100% corrigidas) =================
async def atualizar_loja():
    canal = bot.get_channel(CANAL_LOJA)
    if not canal:
        return
    async for msg in canal.history(limit=10):
        if msg.author == bot.user:
            try:
                await msg.delete()
            except:
                pass
    await canal.send(embed=await montar_embed_loja(), view=LojaButtons())

async def atualizar_vendas():
    canal = bot.get_channel(CANAL_VENDAS)
    if not canal:
        return
    async for msg in canal.history(limit=10):
        if msg.author == bot.user:
            try:
                await msg.delete()
            except:
                pass
    await canal.send(embed=await montar_embed_vendas())

# ================= WEBHOOK MP =================
async def webhook_mp(request):
    try:
        data = await request.json()
        pay_id = data.get("data", {}).get("id") if data.get("type") == "payment" else None
        if pay_id and pay_id in pedidos_pendentes:
            info = sdk.payment().get(pay_id)
            if info["response"].get("status") == "approved":
                pedido_id = pedidos_pendentes[pay_id]
                async with db.acquire() as conn:
                    pedido = await conn.fetchrow("SELECT * FROM pedidos WHERE id=$1", pedido_id)
                    if pedido and pedido["status"] == "pendente":
                        user = await bot.fetch_user(pedido["user_id"])
                        produtos = await get_produtos()
                        produto = produtos.get(pedido["produto_id"])
                        if produto:
                            guild = await get_guild()
                            if guild:
                                await update_pedido(pedido_id, "aprovado")
                                await add_venda(produto["preco"])
                                await entregar_produto(user, dict(produto), pedido_id, guild)
                                await atualizar_vendas()
    except Exception as e:
        print(f"Webhook MP: {e}")
    return web.Response(status=200)

async def start_server():
    app = web.Application()
    app.router.add_post("/webhook", webhook_mp)
    app.router.add_get("/health", lambda r: web.Response(text="OK — NEXZY STORE"))
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
    except:
        pass

@bot.command(name="vendas")
async def cmd_vendas(ctx):
    if not any(r.id == CARGO_DONO for r in ctx.author.roles):
        return
    await ctx.send(embed=await montar_embed_vendas())
    try:
        await ctx.message.delete()
    except:
        pass

@bot.command(name="upload")
async def cmd_upload(ctx, produto_id: str = None):
    if not any(r.id == CARGO_DONO for r in ctx.author.roles):
        return await ctx.reply("❌ Sem permissão.", delete_after=5)
    if not produto_id:
        return await ctx.reply("❌ Uso: `!upload <produto_id>` com arquivo anexado.", delete_after=15)
    if not ctx.message.attachments:
        return await ctx.reply("❌ Nenhum arquivo anexado.", delete_after=10)
    produtos = await get_produtos()
    if produto_id not in produtos:
        return await ctx.reply(f"❌ Produto `{produto_id}` não encontrado.\nIDs: {', '.join(produtos.keys())}", delete_after=15)
    att = ctx.message.attachments[0]
    if att.size/1024/1024 > 25:
        return await ctx.reply(f"❌ Arquivo muito grande: **{att.size/1024/1024:.1f} MB**", delete_after=15)
    msg = await ctx.reply(f"⏳ Salvando **{att.filename}**...")
    try:
        dados = await att.read()
        await salvar_arquivo_produto(produto_id, att.filename, dados)
        await msg.edit(content=f"✅ Arquivo **{att.filename}** salvo!\nProduto: `{produto_id}` — **{produtos[produto_id]['nome']}**")
        await atualizar_loja()
        await log_admin("Upload de Arquivo", ctx.author, f"**{att.filename}** • Produto `{produto_id}`")
    except Exception as e:
        await msg.edit(content=f"❌ Erro: {e}")

@bot.command(name="remover_arquivo")
async def cmd_remover_arquivo(ctx, produto_id: str = None):
    if not any(r.id == CARGO_DONO for r in ctx.author.roles):
        return
    if not produto_id:
        return await ctx.reply("❌ Use: `!remover_arquivo <produto_id>`")
    await remover_arquivo_produto(produto_id)
    await ctx.reply(f"✅ Arquivo removido do produto `{produto_id}`.")
    await atualizar_loja()
    await log_admin("Arquivo Removido", ctx.author, f"Produto `{produto_id}`")

@bot.command(name="check7z")
async def cmd_check7z(ctx):
    if not any(r.id == CARGO_DONO for r in ctx.author.roles):
        return
    ok = verificar_7zip()
    if ok:
        result = subprocess.run(["7z", "i"], capture_output=True, text=True, timeout=5)
        await ctx.reply(f"✅ **7-Zip instalado!**\n`{result.stdout.strip()}`")
    else:
        await ctx.reply("❌ **7-Zip NÃO encontrado.**")

@bot.command(name="instalar7z")
async def cmd_instalar7z(ctx):
    """Instala o 7-Zip no servidor Railway"""
    if not any(r.id == CARGO_DONO for r in ctx.author.roles):
        return await ctx.reply("❌ Sem permissão.", delete_after=5)
    
    msg = await ctx.reply("⏳ Instalando 7-Zip... (isso pode levar 30 segundos)")
    
    try:
        # Instala o 7-Zip
        result = subprocess.run(
            ["apt-get", "update"],
            capture_output=True, text=True, timeout=60
        )
        
        result = subprocess.run(
            ["apt-get", "install", "-y", "p7zip-full"],
            capture_output=True, text=True, timeout=120
        )
        
        if result.returncode == 0:
            # Verifica se funciona
            test = subprocess.run(
                ["7z", "--help"],
                capture_output=True, text=True, timeout=5
            )
            
            if test.returncode == 0:
                await msg.edit(content="✅ **7-Zip instalado com sucesso!**\nAgora os testes de entrega vão funcionar!")
                await log_admin("7-Zip Instalado", ctx.author, "Instalação concluída com sucesso")
            else:
                await msg.edit(content="⚠️ Instalação concluída mas 7z não respondeu. Tente reiniciar o bot.")
        else:
            await msg.edit(content=f"❌ Erro na instalação:\n```{result.stderr[:300]}```")
            
    except Exception as e:
        await msg.edit(content=f"❌ Erro ao executar: {e}")

# ================= EVENTOS =================
@bot.event
async def on_ready():
    print(f"✅ Bot online: {bot.user}")
    if not await init_db():
        return
    if not verificar_7zip():
        print("⚠️  7-Zip NÃO encontrado!")
    else:
        print("✅ 7-Zip disponível")
    guild = await get_guild()
    if guild is None:
        print(f"❌ Servidor {GUILD_ID} não encontrado. Configure GUILD_ID!")
    else:
        print(f"✅ Servidor: {guild.name}")
    asyncio.create_task(start_server())
    # só atualiza loja/vendas se guild existir
    if guild:
        await atualizar_loja()
        await atualizar_vendas()
    print("✅ Pronto!")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = interaction.data.get("custom_id", "")
    if custom_id.startswith("check_"):
        pay_id = int(custom_id.split("_")[1])
        await interaction.response.defer(ephemeral=True)
        try:
            info = sdk.payment().get(pay_id)
            status = info["response"].get("status")
            msgs = {"approved": "✅ Pagamento aprovado!", "pending": "⏳ Pendente.", "rejected": "❌ Recusado."}
            await interaction.followup.send(msgs.get(status, f"ℹ️ Status: `{status}`"), ephemeral=True)
        except:
            await interaction.followup.send("❌ Erro ao verificar.", ephemeral=True)
    elif custom_id.startswith("cancel_"):
        await interaction.response.send_message("❌ Pedido cancelado.", ephemeral=True)

# ================= MAIN =================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
