FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY update.py .
COPY system_prompt.txt .
ENV TZ=Europe/Copenhagen
CMD ["python", "main.py"]