# ---- Build stage ----
FROM oven/bun:1 AS build
WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY . .

# FastAPI backend URL, baked in at build time (Vite env).
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL

# vite.config.ts pins the nitro `node-server` preset, so this emits a
# standalone Node server in .output/ (server + public assets).
RUN bun run build

# ---- Runtime stage ----
FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000

COPY --from=build /app/.output ./.output

EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
