import discord
from discord.ext import commands
import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional

# ================= CONFIGURAÇÕES =================
TOKEN = os.getenv("DISCORD_TOKEN")

# IDs - Variáveis de ambiente
ID_CANAL_PAINEL = int(os.getenv("ID_CANAL_PAINEL", "0"))
ID_CARGO_STAFF = int(os.getenv("ID_CARGO_STAFF", "0"))
ID_CANAL_LOGS = int(os.getenv("ID_CANAL_LOGS", "0"))
CATEGORIA_COMPRAS = int(os.getenv("CATEGORIA_COMPRAS", "0"))
CATEGORIA_DENUNCIA = int(os.getenv("CATEGORIA_DENUNCIA", "0"))

# CONFIGURAÇÕES
TEMPO_ESPERA = int(os.getenv("TEMPO_ESPERA", "60"))
MAX_TICKETS_POR_USUARIO = int(os.getenv("MAX_TICKETS_POR_USUARIO", "3"))
# ==================================================================

# Sistema de gerenciamento
cooldowns: Dict[int, float] = {}
active_tickets: Dict[int, List[int]] = {}

def get_active_tickets_count(user_id: int) -> int:
    return len(active_tickets.get(user_id, []))

def add_active_ticket(user_id: int, channel_id: int) -> None:
    if user_id not in active_tickets:
        active_tickets[user_id] = []
    active_tickets[user_id].append(channel_id)

def remove_active_ticket(user_id: int, channel_id: int) -> None:
    if user_id in active_tickets and channel_id in active_tickets[user_id]:
        active_tickets[user_id].remove(channel_id)

async def log_action(guild: discord.Guild, description: str) -> None:
    if ID_CANAL_LOGS and ID_CANAL_LOGS != 0:
        channel = guild.get_channel(ID_CANAL_LOGS)
        if channel and isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title="📝 Log do Sistema",
                description=description,
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            await channel.send(embed=embed)

async def save_transcript(channel: discord.TextChannel, motivo: str = "Não informado") -> Optional[str]:
    messages = []
    async for message in channel.history(limit=200, oldest_first=True):
        timestamp = message.created_at.strftime("%d/%m/%Y %H:%M:%S")
        messages.append(f"[{timestamp}] {message.author.name}: {message.content}")
    
    if not messages:
        return None
    
    transcript = "\n".join(messages)
    filename = f"transcript_{channel.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"Ticket: {channel.name}\n")
        f.write(f"Fechado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
        f.write(f"Motivo: {motivo}\n")
        f.write("="*50 + "\n\n")
        f.write(transcript)
    
    return filename

# ================= BOTÕES =================
class BotaoFechar(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔒 Fechar Ticket", style=discord.ButtonStyle.red, custom_id="botao_fechar_ticket")
    
    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel:
            return
        
        cargo_staff = interaction.guild.get_role(ID_CARGO_STAFF)
        is_staff = cargo_staff in interaction.user.roles if cargo_staff else False
        ticket_owner = interaction.channel.name.split("-")[-1] if interaction.channel.name else ""
        
        if not is_staff and interaction.user.name != ticket_owner:
            return await interaction.response.send_message("❌ Apenas staff ou o criador podem fechar!", ephemeral=True)
        
        await interaction.response.send_message("🔒 Fechando ticket em 5 segundos...")
        
        if isinstance(interaction.channel, discord.TextChannel):
            transcript_file = await save_transcript(interaction.channel)
            
            if transcript_file and ID_CANAL_LOGS and ID_CANAL_LOGS != 0:
                log_channel = interaction.guild.get_channel(ID_CANAL_LOGS)
                if log_channel and isinstance(log_channel, discord.TextChannel):
                    embed = discord.Embed(
                        title="📄 Transcript",
                        description=f"Ticket: {interaction.channel.name}\nFechado por: {interaction.user.mention}",
                        color=discord.Color.blue()
                    )
                    with open(transcript_file, "rb") as f:
                        file = discord.File(f, filename=transcript_file)
                        await log_channel.send(embed=embed, file=file)
                    os.remove(transcript_file)
        
        await asyncio.sleep(5)
        
        for uid, tickets in list(active_tickets.items()):
            if interaction.channel and interaction.channel.id in tickets:
                remove_active_ticket(uid, interaction.channel.id)
                break
        
        if interaction.channel:
            await interaction.channel.delete()

class BotaoAdicionar(discord.ui.Button):
    def __init__(self):
        super().__init__(label="➕ Adicionar Usuário", style=discord.ButtonStyle.success, custom_id="botao_adicionar_usuario")
    
    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return
        
        cargo_staff = interaction.guild.get_role(ID_CARGO_STAFF)
        if not (cargo_staff and cargo_staff in interaction.user.roles):
            return await interaction.response.send_message("❌ Apenas staff pode usar isso!", ephemeral=True)
        
        await interaction.response.send_modal(AdicionarModal())

class AdicionarModal(discord.ui.Modal, title="Adicionar Usuário"):
    user_id = discord.ui.TextInput(label="ID do Usuário", placeholder="Digite o ID do usuário", required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel:
            return
        
        try:
            user = await interaction.guild.fetch_member(int(self.user_id.value))
            if isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True)
                await interaction.response.send_message(f"✅ {user.mention} adicionado!", ephemeral=True)
                await interaction.channel.send(f"➕ {user.mention} foi adicionado por {interaction.user.mention}")
        except:
            await interaction.response.send_message("❌ Usuário não encontrado!", ephemeral=True)

class BotaoTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(BotaoAdicionar())
        self.add_item(BotaoFechar())

# ================= SELECT MENU =================
class SelectTicket(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="💰 Compras", description="Dúvidas sobre compras", emoji="💰", value="compras"),
            discord.SelectOption(label="🚨 Denúncia", description="Denunciar um usuário", emoji="🚨", value="denuncia"),
        ]
        super().__init__(placeholder="Escolha o tipo de ticket...", min_values=1, max_values=1, options=options, custom_id="select_ticket_menu")
    
    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.user:
            return
        
        guild = interaction.guild
        user = interaction.user
        cargo_staff = guild.get_role(ID_CARGO_STAFF)
        
        if not cargo_staff:
            return await interaction.response.send_message("❌ Cargo staff não configurado!", ephemeral=True)
        
        active_count = get_active_tickets_count(user.id)
        if active_count >= MAX_TICKETS_POR_USUARIO:
            return await interaction.response.send_message(f"❌ Você já tem {active_count}/{MAX_TICKETS_POR_USUARIO} tickets ativos!", ephemeral=True)
        
        agora = asyncio.get_event_loop().time()
        if user.id in cooldowns:
            if agora - cooldowns[user.id] < TEMPO_ESPERA:
                restante = int(TEMPO_ESPERA - (agora - cooldowns[user.id]))
                return await interaction.response.send_message(f"⏳ Aguarde {restante}s para abrir outro ticket.", ephemeral=True)
        
        escolha = self.values[0]
        
        if escolha == "compras":
            categoria_id = CATEGORIA_COMPRAS
            nome_escolha = "Compras"
        else:
            categoria_id = CATEGORIA_DENUNCIA
            nome_escolha = "Denúncia"
        
        categoria = guild.get_channel(categoria_id)
        if not categoria or not isinstance(categoria, discord.CategoryChannel):
            return await interaction.response.send_message("❌ Categoria não configurada corretamente!", ephemeral=True)
        
        ticket_name = f"{nome_escolha.lower()}-{user.name}"[:32]
        ticket_name = ticket_name.replace(" ", "-").replace("ç", "c").replace("ã", "a").replace(" ", "")
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            cargo_staff: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        }
        
        try:
            canal = await guild.create_text_channel(name=ticket_name, category=categoria, overwrites=overwrites)
        except Exception as e:
            return await interaction.response.send_message(f"❌ Erro ao criar ticket: {e}", ephemeral=True)
        
        cooldowns[user.id] = agora
        add_active_ticket(user.id, canal.id)
        
        await interaction.response.send_message(f"✅ Ticket criado: {canal.mention}", ephemeral=True)
        
        embed = discord.Embed(
            title=f"🎫 {nome_escolha}",
            description=f"👋 Olá {user.mention}\n\nExplique seu problema e aguarde a staff.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Sistema de Tickets")
        
        await canal.send(content=f"🔔 {cargo_staff.mention}", embed=embed, view=BotaoTicketView())
        await log_action(guild, f"🎫 {user.name} abriu ticket {nome_escolha} → {canal.mention}")

class PainelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SelectTicket())

# ================= BOT =================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    bot.add_view(PainelView())
    bot.add_view(BotaoTicketView())
    
    print(f"✅ Bot de Tickets online: {bot.user}")
    print(f"✅ Conectado em {len(bot.guilds)} servidores")

@bot.command()
@commands.has_permissions(administrator=True)
async def painel(ctx):
    """Cria o painel de tickets"""
    if ctx.channel.id != ID_CANAL_PAINEL:
        return await ctx.send(f"❌ Use no canal <#{ID_CANAL_PAINEL}>", delete_after=5)
    
    embed = discord.Embed(
        title="🎫 Central de Atendimento",
        description="**Clique no menu abaixo e escolha o tipo de ticket**\n\n💰 **Compras** - Dúvidas sobre compras\n🚨 **Denúncia** - Denunciar usuários\n\n⚠️ Aguarde pacientemente o atendimento.",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Sistema de Tickets")
    
    await ctx.send(embed=embed, view=PainelView())
    await ctx.message.delete()

@bot.command()
@commands.has_permissions(administrator=True)
async def fechar(ctx):
    """Fecha o ticket atual"""
    if not ctx.channel.name.startswith(("compras", "denúncia")):
        return await ctx.send("❌ Este não é um canal de ticket!", delete_after=5)
    
    await ctx.send("🔒 Fechando ticket em 5 segundos...")
    
    if isinstance(ctx.channel, discord.TextChannel):
        transcript_file = await save_transcript(ctx.channel)
        
        if transcript_file and ID_CANAL_LOGS and ID_CANAL_LOGS != 0:
            log_channel = ctx.guild.get_channel(ID_CANAL_LOGS)
            if log_channel and isinstance(log_channel, discord.TextChannel):
                embed = discord.Embed(title="📄 Transcript", description=f"Ticket: {ctx.channel.name}\nFechado por: {ctx.author.mention}", color=discord.Color.blue())
                with open(transcript_file, "rb") as f:
                    file = discord.File(f, filename=transcript_file)
                    await log_channel.send(embed=embed, file=file)
                os.remove(transcript_file)
    
    await asyncio.sleep(5)
    
    for uid, tickets in list(active_tickets.items()):
        if ctx.channel.id in tickets:
            remove_active_ticket(uid, ctx.channel.id)
            break
    
    await ctx.channel.delete()

@bot.command()
@commands.has_permissions(administrator=True)
async def stats(ctx):
    """Mostra estatísticas"""
    total = sum(len(tickets) for tickets in active_tickets.values())
    embed = discord.Embed(title="📊 Estatísticas", color=discord.Color.green())
    embed.add_field(name="🎫 Tickets Ativos", value=str(total), inline=True)
    embed.add_field(name="👥 Usuários com Ticket", value=str(len(active_tickets)), inline=True)
    embed.add_field(name="⏰ Cooldown", value=f"{TEMPO_ESPERA}s", inline=True)
    embed.add_field(name="📈 Limite por Usuário", value=str(MAX_TICKETS_POR_USUARIO), inline=True)
    await ctx.send(embed=embed, delete_after=10)

# ================= START =================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERRO: Token não configurado!")
        print("📝 Configure DISCORD_TOKEN no Railway Variables")
    else:
        print("🚀 Iniciando Bot de Tickets...")
        bot.run(TOKEN)
