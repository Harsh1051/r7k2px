# Build stage
FROM node:20-alpine AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci || npm install

COPY . .

# Optional build arg for public API URL. If empty, relative reverse proxy is used.
ARG VITE_API_URL=""
ENV VITE_API_URL=${VITE_API_URL}

RUN npm run build

# Production stage
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
