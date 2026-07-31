# Build the search React app (base /search/)
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

# Telemetry (Faro) is gated at build by `import.meta.env.VITE_TELEMETRY === '1'`
# (see main.tsx). Canary images build with `--build-arg VITE_TELEMETRY=1` to
# include Faro; a normal prod build leaves it unset → empty → telemetry excluded.
ARG VITE_TELEMETRY
ENV VITE_TELEMETRY=${VITE_TELEMETRY}

RUN npm run build

# Serve the static app; the main frontend nginx proxies /search/ to this container.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html/search

# Regenerates /search/config.js from FRONTEND_OIDC_* env at container start.
COPY docker-entrypoint.sh /docker-entrypoint-searchui.sh
RUN chmod +x /docker-entrypoint-searchui.sh
ENTRYPOINT ["/docker-entrypoint-searchui.sh"]

EXPOSE 80
