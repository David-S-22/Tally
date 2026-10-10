FROM nginx:alpine
EXPOSE 3000

# Preserve Nginx variables such as $host and $proxy_host while substituting
# only the service configuration variables in the template.
ENV NGINX_ENVSUBST_TEMPLATE_VARS='$PORT $ANOMALIES_FRONTEND_URL $TRANSACTIONS_FRONTEND_URL $SAVINGS_FRONTEND_URL $BILLS_FRONTEND_URL $BUDGETS_FRONTEND_URL'

COPY public/ /app/public/
COPY nginx.conf /etc/nginx/templates/default.conf.template
