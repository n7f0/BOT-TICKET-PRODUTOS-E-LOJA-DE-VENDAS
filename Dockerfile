FROM python:3.11-slim

WORKDIR /app

# Instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar os bots
COPY main.py .
COPY bot-novidades.py .

# Comando para rodar os dois bots
CMD python main.py & python bot-novidades.py
