import os
import discord
from discord.ext import commands
from discord.ui import Modal, TextInput, Button, View

# ================= CONFIGURAÇÕES =================
TOKEN = os.getenv("TOKEN")  # Pega do ambiente - NUNCA coloque o token aqui!
CANAL_PRODUTOS_ID = 1494142109123346542

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
        placeholder="Ex: @Membros  ou  @Equipe  ou  ID do cargo",
        required=True,
        max_length=100
    )
    
    cargo2 = TextInput(
        label="👥 SEGUNDO CARGO",
        placeholder="Ex: @Vips  ou  @Apoiadores  ou  ID do cargo",
        required=True,
        max_length=100
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        canal_produtos = bot.get_channel(CANAL_PRODUTOS_ID)
        
        if not canal_produtos:
            await interaction.followup.send(
                f"❌ Canal de produtos não encontrado! Verifique o ID: {CANAL_PRODUTOS_ID}",
                ephemeral=True
            )
            return
        
        if not isinstance(canal_produtos, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("❌ O canal de produtos não é um canal de texto válido!", ephemeral=True)
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
        
        embed.color = discord.Color.from_rgb(59, 130, 246)
        
        try:
            mensagem = f"{cargo1_menção} {cargo2_menção}\n"
            await canal_produtos.send(mensagem, embed=embed)
            await interaction.followup.send(
                f"✅ Novidade publicada com sucesso em {canal_produtos.mention}!\n"
                f"📌 Cargos mencionados: {cargo1_menção} {cargo2_menção}",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Erro ao publicar: {str(e)}", ephemeral=True)
    
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

# ================= COMANDO !NOVIDADES =================
@bot.command(name="novidades")
async def comando_novidades(ctx):
    """Mostra o botão para criar novidades"""
    
    embed = discord.Embed(
        title="✨ **SISTEMA DE NOVIDADES**",
        description="Clique no botão abaixo para criar uma nova novidade!\n\n"
                   "**Você poderá:**\n"
                   "📌 Escrever título e conteúdo\n"
                   "👥 Mencionar **DOIS CARGOS** diferentes\n"
                   "🎨 O bot publica com design automático\n\n"
                   f"📢 **As novidades serão postadas em:** {bot.get_channel(CANAL_PRODUTOS_ID).mention if bot.get_channel(CANAL_PRODUTOS_ID) else 'Canal configurado'}",
        color=discord.Color.green()
    )
    
    embed.set_footer(text="Clique no botão abaixo para começar • Sistema de Novidades")
    embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild.icon else None)
    
    cargos_importantes = []
    for role in ctx.guild.roles:
        if not role.is_default() and len(cargos_importantes) < 5:
            cargos_importantes.append(role.mention)
    
    if cargos_importantes:
        embed.add_field(
            name="📌 Cargos disponíveis no servidor",
            value=", ".join(cargos_importantes[:5]) + ("..." if len(cargos_importantes) > 5 else ""),
            inline=False
        )
    
    view = NovidadeView()
    await ctx.send(embed=embed, view=view)

# ================= COMANDO PARA TESTAR O CANAL =================
@bot.command(name="testar_canal")
async def testar_canal(ctx):
    """Testa se o canal de produtos está configurado corretamente"""
    canal = bot.get_channel(CANAL_PRODUTOS_ID)
    if canal:
        embed = discord.Embed(
            title="✅ Canal Configurado",
            description=f"O canal de produtos é: {canal.mention}\n"
                       f"ID: `{CANAL_PRODUTOS_ID}`\n"
                       f"Tipo: {type(canal).__name__}",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="❌ Erro na Configuração",
            description=f"Não foi possível encontrar o canal com ID: `{CANAL_PRODUTOS_ID}`\n"
                       f"Verifique se o ID está correto e se o bot está no servidor certo.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)

# ================= EVENTO DE INICIALIZAÇÃO =================
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Bot logado como {bot.user}")
    print(f"📌 Comandos sincronizados!")
    print(f"📢 Canal de produtos configurado: {CANAL_PRODUTOS_ID}")
    
    canal = bot.get_channel(CANAL_PRODUTOS_ID)
    if canal:
        print(f"✅ Canal de produtos encontrado: {canal.name}")
    else:
        print(f"❌ ATENÇÃO: Canal de produtos NÃO encontrado! Verifique o ID.")
    
    print(f"\n🎯 Comandos disponíveis:")
    print(f"   !novidades - Mostra o botão para criar novidades")
    print(f"   !testar_canal - Testa a configuração do canal")

if __name__ == "__main__":
    bot.run(TOKEN)