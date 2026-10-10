FROM nginx:alpine
EXPOSE 3004

ENV NGINX_ENVSUBST_TEMPLATE_VARS='$PORT $ANOMALIES_BACKEND_URL'

COPY public/ /app/public/
COPY nginx.conf /etc/nginx/templates/default.conf.template
