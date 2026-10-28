FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge_core.py app.py export_xlsx.py ./
RUN mkdir -p /data /recordings
EXPOSE 8890
# Port 8890 (not 8090): the bridge runs with host networking (see
# deploy/docker-compose.yml) so uvicorn binds the host port directly
# and the desk URL stays http://server:8890/.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8890"]
