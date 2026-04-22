FROM python:3.11-slim

WORKDIR /app

# Instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todos os bots
COPY bots/ ./bots/

# Criar script para rodar os 3 bots
RUN echo '#!/bin/bash\n\
python bots/main.py &\n\
python bots/bot-novidades.py &\n\
python bots/bot.py\n\
' > start.sh && chmod +x start.sh

CMD ["./start.sh"]