import discord
from discord.ext import commands, tasks
from discord import Embed, Color
import json
import aiohttp
import mercadopago
import uuid
import asyncio
from datetime import datetime, timezone
from aiohttp import web

# ================= CONFIG =================
CARGO_DONO = 1486353931087908874
CANAL_STATS = 1494068657762996325
CANAL_FALHAS = 1494068657762996326
WEBHOOK_LOG = "https://discord.com/api/webhooks/1492726811891859526/O0lg0DRR_Ftmfj5wUTgxvZg0da1RyWdeHLtSKQAe1XMaxrnY29fbnLoKodQFBccJs_o_"

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

config.setdefault("produtos", {})
config.setdefault("estatisticas", {})
config.setdefault("pedidos", {})
config.setdefault("entregues", [])
config.setdefault("pedidos_pendentes_entrega", {})

def salvar():
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def formatar_preco(valor):
    valor = float(valor)
    if valor.is_integer():
        return str(int(valor))
    return f"{valor:.2f}".rstrip("0").rstrip(".").replace(".", ",")

def eh_dono(interaction: discord.Interaction) -> bool:
    return any(r.id == CARGO_DONO for r in interaction.user.roles)

sdk = mercadopago.SDK(config["mercado_pago_token"])
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

pedidos_pendentes = {}

# ================= EMBEDS =================
def montar_embed_privado():
    vendas = config.get("estatisticas", {}).get("vendas", 0)
    faturamento = config.get("estatisticas", {}).get("faturamento", 0.0)

    embed = Embed(
        title="📊 PAINEL PRIVADO",
        description="(Apenas você vê isso)",
        color=Color.dark_gold()
    )
    embed.add_field(name="📦 Total de Vendas", value=str(vendas), inline=True)
    embed.add_field(name="💰 Faturamento", value=f"R$ {formatar_preco(faturamento)}", inline=True)
    return embed

def montar_embed_loja():
    embed = Embed(
        title="🛒 NEXZY STORE",
        description="💎 Compre automaticamente via PIX",
        color=Color.dark_blue()
    )
    embed.set_image(url="https://media.discordapp.net/attachments/1491808878562643998/1491808965170958396/e6876514-c5ae-477f-a84b-d7b7db0c01e5.png")

    for k, p in config["produtos"].items():
        embed.add_field(
            name=f"{p.get('emoji', '🛒')} {p['nome']}",
            value=f"💰 R$ {formatar_preco(p['preco'])}\n📦 Entrega automática\n🆔 ID: `{k}`",
            inline=True
        )

    return embed

# ================= UTIL =================
async def atualizar_painel_privado():
    canal = bot.get_channel(CANAL_STATS)
    if canal is None:
        return

    embed = montar_embed_privado()
    msg_id = config.get("estatisticas", {}).get("mensagem_id")

    try:
        if msg_id:
            msg = await canal.fetch_message(msg_id)
            await msg.edit(embed=embed)
        else:
            msg = await canal.send(embed=embed)
            config["estatisticas"]["mensagem_id"] = msg.id
            salvar()
    except Exception:
        msg = await canal.send(embed=embed)
        config["estatisticas"]["mensagem_id"] = msg.id
        salvar()

async def atualizar_painel_loja():
    canal = bot.get_channel(CANAL_STATS)
    if canal is None:
        return

    embed = montar_embed_loja()
    msg_id = config.get("estatisticas", {}).get("painel_loja_id")

    try:
        if msg_id:
            msg = await canal.fetch_message(msg_id)
            await msg.edit(embed=embed, view=PainelPrincipal())
        else:
            msg = await canal.send(embed=embed, view=PainelPrincipal())
            config["estatisticas"]["painel_loja_id"] = msg.id
            salvar()
    except Exception:
        msg = await canal.send(embed=embed, view=PainelPrincipal())
        config["estatisticas"]["painel_loja_id"] = msg.id
        salvar()

async def notificar_falha(user, titulo, descricao):
    canal = bot.get_channel(CANAL_FALHAS)
    if not canal:
        return
    embed = Embed(title=titulo, description=descricao, color=Color.red(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Usuário", value=f"{user} ({user.id})", inline=False)
    try:
        await canal.send(embed=embed)
    except Exception:
        pass

# ================= LOG =================
class LogView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(discord.ui.Button(label="👤 Ver Perfil", url=f"https://discord.com/users/{user_id}"))

async def enviar_log(tipo, usuario=None, produto=None, valor=None, extra=None):
    titulo = {"pedido": "🟡 NOVO PEDIDO", "venda": "🟢 VENDA APROVADA", "erro": "🔴 ERRO"}[tipo]
    cor = {"pedido": Color.gold(), "venda": Color.green(), "erro": Color.red()}[tipo]

    embed = Embed(title=titulo, color=cor, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Nexzy Store • Sistema Automático")

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
            webhook = discord.Webhook.from_url(WEBHOOK_LOG, session=session)
            await webhook.send(embed=embed, view=LogView(usuario.id) if usuario else None)
    except Exception as e:
        print(f"[ERRO LOG] {e}")

# ================= PAGAMENTO =================
def criar_pagamento(user_id, produto):
    resposta = sdk.payment().create({
        "transaction_amount": float(produto["preco"]),
        "description": produto["nome"],
        "payment_method_id": "pix",
        "payer": {"email": f"user_{user_id}@email.com"}
    })
    return resposta["response"]

async def processar_compra(interaction: discord.Interaction, key: str):
    produto = config["produtos"].get(key)
    if not produto:
        await interaction.response.send_message("❌ Produto não encontrado.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        loop = asyncio.get_running_loop()
        pagamento = await loop.run_in_executor(None, criar_pagamento, interaction.user.id, produto)
    except Exception as e:
        await interaction.followup.send(f"❌ Erro ao gerar pagamento: `{e}`", ephemeral=True)
        return

    try:
        pix = pagamento["point_of_interaction"]["transaction_data"]["qr_code"]
    except Exception:
        await interaction.followup.send("❌ Erro ao extrair código PIX.", ephemeral=True)
        return

    pid = str(pagamento["id"])
    pedido = {
        "user_id": interaction.user.id,
        "produto": produto,
        "produto_id": key,
        "status": "pendente",
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    pedidos_pendentes[pid] = pedido
    config["pedidos"][pid] = pedido
    salvar()

    await enviar_log("pedido", interaction.user, produto, produto["preco"])

    embed = Embed(title="💳 Pagamento PIX", color=Color.green())
    embed.add_field(name="Produto", value=produto["nome"], inline=True)
    embed.add_field(name="Valor", value=f"R$ {formatar_preco(produto['preco'])}", inline=True)
    embed.add_field(name="🔑 Copia e Cola", value=f"```{pix}```", inline=False)
    embed.set_footer(text="Após o pagamento você receberá o produto via DM.")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ================= MODAIS =================
class AddModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Adicionar Produto")
        self.nome = discord.ui.TextInput(label="Nome")
        self.preco = discord.ui.TextInput(label="Preço (ex: 10)")
        self.emoji = discord.ui.TextInput(label="Emoji", required=False)
        self.link = discord.ui.TextInput(label="Link de entrega")
        self.add_item(self.nome)
        self.add_item(self.preco)
        self.add_item(self.emoji)
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_val = float(self.preco.value.replace(",", "."))
        except ValueError:
            return await interaction.response.send_message("❌ Preço inválido.", ephemeral=True)

        pid = f"produto_{uuid.uuid4().hex[:6]}"
        config["produtos"][pid] = {
            "nome": self.nome.value,
            "preco": preco_val,
            "emoji": self.emoji.value or "🛒",
            "link": self.link.value
        }
        salvar()
        await atualizar_painel_loja()
        await interaction.response.send_message(f"✅ Produto **{self.nome.value}** adicionado!\nID: `{pid}`", ephemeral=True)

class EditModal(discord.ui.Modal):
    def __init__(self, key: str):
        super().__init__(title="Editar Produto")
        self.key = key
        p = config["produtos"][key]
        self.nome = discord.ui.TextInput(label="Novo nome", default=p["nome"], required=True)
        self.preco = discord.ui.TextInput(label="Novo valor", default=formatar_preco(p["preco"]), required=True)
        self.add_item(self.nome)
        self.add_item(self.preco)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_val = float(self.preco.value.replace(",", "."))
        except ValueError:
            return await interaction.response.send_message("❌ Valor inválido.", ephemeral=True)

        config["produtos"][self.key]["nome"] = self.nome.value
        config["produtos"][self.key]["preco"] = preco_val
        salvar()
        await atualizar_painel_loja()
        await interaction.response.send_message(f"✅ Produto `{self.key}` atualizado com sucesso!", ephemeral=True)

# ================= SELECTS =================
class SelectProdutos(discord.ui.Select):
    def __init__(self):
        options = []
        for key, produto in list(config["produtos"].items())[:25]:
            options.append(discord.SelectOption(
                label=produto["nome"][:100],
                description=f"R$ {formatar_preco(produto['preco'])}",
                emoji=produto.get("emoji", "🛒"),
                value=key
            ))
        super().__init__(placeholder="Selecione um produto...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await processar_compra(interaction, self.values[0])

class SelectEditarProdutos(discord.ui.Select):
    def __init__(self):
        options = []
        for key, produto in list(config["produtos"].items())[:25]:
            options.append(discord.SelectOption(
                label=produto["nome"][:100],
                description=f"Editar R$ {formatar_preco(produto['preco'])}",
                emoji=produto.get("emoji", "🛒"),
                value=key
            ))
        super().__init__(placeholder="Escolha o produto para editar...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditModal(self.values[0]))

class SelectRemoverProdutos(discord.ui.Select):
    def __init__(self):
        options = []
        for key, produto in list(config["produtos"].items())[:25]:
            options.append(discord.SelectOption(
                label=produto["nome"][:100],
                description=f"Remover R$ {formatar_preco(produto['preco'])}",
                emoji=produto.get("emoji", "🛒"),
                value=key
            ))
        super().__init__(placeholder="Escolha o produto para remover...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.value
        if key not in config["produtos"]:
            return await interaction.response.send_message("❌ Produto não encontrado.", ephemeral=True)
        del config["produtos"][key]
        salvar()
        await atualizar_painel_loja()
        await interaction.response.send_message("✅ Produto removido com sucesso.", ephemeral=True)

# ================= VIEWS =================
class BotaoVoltarPrincipal(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔙 Voltar", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=montar_embed_loja(), view=PainelPrincipal(), ephemeral=True)

class BotaoVoltarAdmin(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔙 Voltar", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Menu administrativo:", view=ViewAdmin(), ephemeral=True)

class ViewProdutos(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(SelectProdutos())
        self.add_item(BotaoVoltarPrincipal())

class ViewEditarProdutos(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(SelectEditarProdutos())
        self.add_item(BotaoVoltarAdmin())

class ViewRemoverProdutos(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(SelectRemoverProdutos())
        self.add_item(BotaoVoltarAdmin())

class BotaoAbrirProdutos(discord.ui.Button):
    def __init__(self):
        super().__init__(label="📦 Produtos", style=discord.ButtonStyle.success, custom_id="btn_produtos")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Escolha um produto:", view=ViewProdutos(), ephemeral=True)

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
        if not eh_dono(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_modal(AddModal())

class BotaoEditar(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✏️ Editar", style=discord.ButtonStyle.secondary, custom_id="admin_edit")

    async def callback(self, interaction: discord.Interaction):
        if not eh_dono(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_message("Selecione o produto para editar:", view=ViewEditarProdutos(), ephemeral=True)

class BotaoRemover(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🗑️ Remover", style=discord.ButtonStyle.danger, custom_id="admin_remove")

    async def callback(self, interaction: discord.Interaction):
        if not eh_dono(interaction):
            return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        await interaction.response.send_message("Selecione o produto para remover:", view=ViewRemoverProdutos(), ephemeral=True)

class ViewAdmin(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(BotaoAdd())
        self.add_item(BotaoEditar())
        self.add_item(BotaoRemover())
        self.add_item(BotaoVoltarPrincipal())

class PainelPrincipal(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BotaoAbrirProdutos())
        self.add_item(BotaoAdmin())

# ================= WEBHOOK =================
async def mp_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)

    if data.get("type") == "payment":
        pid = str(data["data"]["id"])
        try:
            pagamento_info = sdk.payment().get(pid)
            status = pagamento_info["response"].get("status")
        except Exception as e:
            print(f"[ERRO verificar pagamento] {e}")
            return web.Response(status=200)

        if status == "approved":
            if pid in config.get("entregues", []):
                return web.Response(status=200)

            pedido = pedidos_pendentes.pop(pid, None) or config.get("pedidos", {}).pop(pid, None)
            if pedido:
                try:
                    user = await bot.fetch_user(pedido["user_id"])
                    produto = pedido["produto"]
                    produto_id = pedido["produto_id"]

                    embed = Embed(
                        title="🧾 RECIBO DE COMPRA",
                        description="Seu pagamento foi aprovado com sucesso!",
                        color=Color.green(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    embed.add_field(name="📦 Produto", value=f"**{produto['nome']}**", inline=False)
                    embed.add_field(name="💰 Valor", value=f"R$ {formatar_preco(produto['preco'])}", inline=True)
                    embed.add_field(name="🆔 ID do Produto", value=f"`{produto_id}`", inline=True)
                    embed.add_field(name="🔗 Entrega", value=f"[Clique aqui]({produto['link']})", inline=False)
                    embed.set_footer(text="Nexzy Store • Obrigado pela compra ❤️")

                    entregue = False
                    try:
                        await user.send(embed=embed)
                        entregue = True
                    except discord.Forbidden:
                        await enviar_log("erro", user, produto, produto["preco"], extra="DM fechada / bloqueada")
                        await notificar_falha(user, "❌ Falha na entrega", "Usuário com DM fechada ou bloqueada.")
                    except Exception as e:
                        await enviar_log("erro", user, produto, produto["preco"], extra=str(e))
                        await notificar_falha(user, "❌ Falha na entrega", f"Erro ao enviar DM: {e}")

                    config["estatisticas"]["vendas"] = config["estatisticas"].get("vendas", 0) + 1
                    config["estatisticas"]["faturamento"] = config["estatisticas"].get("faturamento", 0.0) + float(produto["preco"])
                    salvar()

                    if entregue:
                        config["entregues"].append(pid)
                        salvar()
                        await enviar_log("venda", user, produto, produto["preco"])
                    else:
                        config["pedidos_pendentes_entrega"][pid] = pedido
                        salvar()

                    await atualizar_painel_privado()
                except Exception as e:
                    print(f"[ERRO entrega] {e}")

    return web.Response(status=200)

async def start_web():
    app = web.Application()
    app.router.add_post("/mp", mp_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("Webhook MP rodando na porta 8080")

# ================= COMANDOS =================
@bot.command()
async def loja(ctx):
    await ctx.send(embed=montar_embed_loja(), view=PainelPrincipal())

# ================= TASK =================
@tasks.loop(minutes=2)
async def atualizar_sistema():
    await atualizar_painel_privado()

# ================= EVENTS =================
@bot.e