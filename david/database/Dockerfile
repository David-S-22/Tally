FROM python:3.14

WORKDIR /app

COPY ./david/database ./database
COPY ./shared/backend ./shared/backend
COPY ./david/database/requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r ./requirements.txt

RUN mkdir -p /app/data

EXPOSE 6002

CMD ["python", "-m", "database.app"]
