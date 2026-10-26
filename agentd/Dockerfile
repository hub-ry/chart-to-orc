FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agentd.py ./
ENV WORKSPACE=/workspace
ENTRYPOINT ["python", "agentd.py"]
