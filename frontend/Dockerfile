# Build the React app
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Build-time app version (git sha) for Faro telemetry `app.version`; vite.config
# reads process.env.APP_VERSION via the `__APP_VERSION__` define. CI passes
# `--build-arg APP_VERSION=sha-<gitsha>`.
ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION}

RUN npm run build

# Serve with nginx and reverse-proxy /api to the backend
FROM nginx:1.27-alpine

RUN apk add --no-cache gettext

COPY nginx.prod.conf.template /etc/nginx/templates/default.conf.template
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
