FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todos os bots (estão na raiz)
COPY main.py .
COPY bot-novidades.py .
COPY bot.py .

# Criar script para rodar os 3 bots
RUN echo '#!/bin/bash\n\
python main.py &\n\
python bot-novidades.py &\n\
python bot.py\n\
' > start.sh && chmod +x start.sh

CMD ["./start.sh"]