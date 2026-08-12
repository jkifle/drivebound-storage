FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY . .
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ARG NEXT_PUBLIC_APP_URL=http://localhost:3000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL \
    WRANGLER_LOG_PATH=.wrangler/wrangler.log
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production \
    WRANGLER_LOG_PATH=.wrangler/wrangler.log
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "run", "start"]
