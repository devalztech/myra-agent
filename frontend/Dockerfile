# ---- Build stage ----
FROM oven/bun:1 AS build
WORKDIR /app

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY . .

# Future FastAPI backend URL, baked in at build time (Vite env).
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL

RUN bun run build

# ---- Runtime stage ----
FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000

COPY --from=build /app/dist ./dist
COPY --from=build /app/docker ./docker

EXPOSE 3000
CMD ["node", "docker/server.mjs"]
