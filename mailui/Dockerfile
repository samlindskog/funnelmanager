# Build the mail React app (base /mail/)
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION}
RUN npm run build

# Serve the static app; the main frontend nginx proxies /mail/ to this container.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html/mail

# Regenerates /mail/config.js from FRONTEND_OIDC_* env at container start.
COPY docker-entrypoint.sh /docker-entrypoint-mailui.sh
RUN chmod +x /docker-entrypoint-mailui.sh
ENTRYPOINT ["/docker-entrypoint-mailui.sh"]

EXPOSE 80
