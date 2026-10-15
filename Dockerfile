# Azure Functions host + Python 3.12 worker. Runs function_app.py unchanged,
# including function-key authorization, on any Docker host (e.g. a VPS).
FROM mcr.microsoft.com/azure-functions/python:4-python3.12

ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    AzureFunctionsJobHost__Logging__Console__IsEnabled=true \
    AzureWebJobsSecretStorageType=files \
    FUNCTIONS_WORKER_RUNTIME=python \
    DOWNLOAD_DIR=/tmp/downloads \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt /
RUN pip install -r /requirements.txt

COPY host.json function_app.py firecrawl_client.py attachment_links.py \
     attachment_downloader.py tender_artifacts.py onedrive_client.py \
     /home/site/wwwroot/

# The Functions host listens on port 80 inside the container.
EXPOSE 80
