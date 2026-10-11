# Bills frontend (:3005) - static HTML/HTMX shell; nginx proxies /api/ and /ui/ to bills-backend.
FROM nginx:alpine

COPY index.html /usr/share/nginx/html/index.html
COPY css/ /usr/share/nginx/html/css/
COPY js/ /usr/share/nginx/html/js/
COPY nginx.conf /etc/nginx/templates/default.conf.template

EXPOSE 80
