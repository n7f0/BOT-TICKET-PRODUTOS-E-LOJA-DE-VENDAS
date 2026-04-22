import os
import discord
from discord.ext import commands
from discord.ui import Modal, TextInput, Button, View

# ================= CONFIGURAÇÕES =================
TOKEN = os.getenv("NOVIDADES_TOKEN")
CANAL_PRODUTOS_ID = int(os.getenv("CANAL_PRODUTOS_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ================= MODAL PARA CRIAR NOVIDADE =================
class NovidadeModal(Modal, title="📢 Criar Nova Novidade"):
    
    titulo = TextInput(
        label="📌 TÍTULO DA NOVIDADE",
        placeholder="Ex: Lançamento Incrível!",
        required=True,
        max_length=100
    )
    
    conteudo = TextInput(
        label="📝 CONTEÚDO",
        placeholder="Descreva sua novidade aqui...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )
    
    cargo1 = TextInput(
        label="👥 PRIMEIRO CARGO",
        placeholder="Ex: @Membros ou ID do cargo",
        required=True,
        max_length=100
    )
    
    cargo2 = TextInput(
        label="👥 SEGUNDO CARGO",
        placeholder="Ex: @Vips ou ID do cargo",
        required=True,
        max_length=100
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        canal_produtos = bot.get_channel(CANAL_PRODUTOS_ID)
        
        if not canal_produtos:
            await interaction.followup.send(
                f"❌ Canal de produtos não encontrado! ID: {CANAL_PRODUTOS_ID}",
                ephemeral=True
            )
            return
        
        if not isinstance(canal_produtos, discord.TextChannel):
            await interaction.followup.send("❌ Canal inválido!", ephemeral=True)
            return
        
        cargo1_menção = self.formatar_mencao_cargo(self.cargo1.value, interaction.guild)
        cargo2_menção = self.formatar_mencao_cargo(self.cargo2.value, interaction.guild)
        
        embed = discord.Embed(
            title=f"📢 **{self.titulo.value}**",
            description=self.conteudo.value,
            color=discord.Color.blue()
        )
        
        embed.add_field(name="📅 Publicado por", value=interaction.user.mention, inline=True)
        embed.add_field(name="⏰ Data", value=discord.utils.format_dt(interaction.created_at, "F"), inline=True)
        embed.set_footer(text="✨ Novidade oficial", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        
        if interaction.guild and interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        
        try:
            mensagem = f"{cargo1_menção} {cargo2_menção}\n"
            await canal_produtos.send(mensagem, embed=embed)
            await interaction.followup.send(
                f"✅ Novidade publicada em {canal_produtos.mention}!",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {str(e)}", ephemeral=True)
    
    def formatar_mencao_cargo(self, texto: str, guild: discord.Guild) -> str:
        texto = texto.strip()
        
        if texto.startswith("<@&") and texto.endswith(">"):
            return texto
        
        if texto.isdigit():
            cargo = guild.get_role(int(texto))
            if cargo:
                return cargo.mention
            return f"@Cargo_{texto}"
        
        if texto.startswith("@"):
            nome_cargo = texto[1:]
            cargo = discord.utils.get(guild.roles, name=nome_cargo)
            if cargo:
                return cargo.mention
            return texto
        
        cargo = discord.utils.get(guild.roles, name=texto)
        if cargo:
            return cargo.mention
        
        return texto

# ================= BOTÃO =================
class NovidadeButton(Button):
    def __init__(self):
        super().__init__(
            label="📢 Criar Novidade",
            style=discord.ButtonStyle.primary,
            emoji="✨",
            custom_id="botao_novidade"
        )
    
    async def callback(self, interaction: discord.Interaction):
        modal = NovidadeModal()
        await interaction.response.send_modal(modal)

class NovidadeView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(NovidadeButton())

# ================= COMANDOS =================
@bot.command(name="novidades")
async def comando_novidades(ctx):
    """Mostra o botão para criar novidades"""
    
    embed = discord.Embed(
        title="✨ **SISTEMA DE NOVIDADES**",
        description="Clique no botão abaixo para criar uma nova novidade!\n\n"
                   "**Você poderá:**\n"
                   "📌 Escrever título e conteúdo\n"
                   "👥 Mencionar **DOIS CARGOS** diferentes\n"
                   "🎨 O bot publica com design automático",
        color=discord.Color.green()
    )
    
    embed.set_footer(text="Clique no botão abaixo para começar")
    
    view = NovidadeView()
    await ctx.send(embed=embed, view=view)

@bot.command(name="testar_canal")
async def testar_canal(ctx):
    """Testa se o canal está configurado"""
    canal = bot.get_channel(CANAL_PRODUTOS_ID)
    if canal:
        embed = discord.Embed(
            title="✅ Canal Configurado",
            description=f"Canal: {canal.mention}\nID: `{CANAL_PRODUTOS_ID}`",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Erro",
            description=f"Canal não encontrado! ID: `{CANAL_PRODUTOS_ID}`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)

@bot.event
async def on_ready():
    print(f"✅ Bot de Novidades online: {bot.user}")
    print(f"📢 Canal de produtos: {CANAL_PRODUTOS_ID}")

if __name__ == "__main__":
    if not TOKEN:
        print("❌ Token do bot de novidades não configurado!")
    else:
        print("🚀 Iniciando Bot de Novidades...")
        bot.run(TOKEN)
