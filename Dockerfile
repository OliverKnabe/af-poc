FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5050
CMD ["python3", "-m", "flask", "--app", "app", "run", "--host", "0.0.0.0", "--port", "5050"]
