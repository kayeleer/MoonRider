# Multi-stage build: compile the webpack bundles on Node 22, serve as pure
# static files with nginx. The one trick that makes webpack 4 work on modern
# Node is NODE_OPTIONS=--openssl-legacy-provider (md4 hashing vs OpenSSL 3).
FROM node:22-bookworm AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ENV NODE_ENV=production
ENV NODE_OPTIONS=--openssl-legacy-provider
RUN npx webpack

FROM nginx:alpine
COPY --from=build /app/index.html /usr/share/nginx/html/
COPY --from=build /app/assets /usr/share/nginx/html/assets
COPY --from=build /app/build /usr/share/nginx/html/build
COPY --from=build /app/vendor /usr/share/nginx/html/vendor
EXPOSE 80
