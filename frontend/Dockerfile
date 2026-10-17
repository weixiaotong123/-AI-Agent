# 阶段 1：构建 Vue 应用
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

# Docker 环境下 API 地址走 Nginx 代理
ENV VITE_API_BASE_URL=/api

RUN npm run build

# 阶段 2：Nginx 托管静态文件
FROM nginx:alpine

# 复制构建产物
COPY --from=build /app/dist /usr/share/nginx/html

# 复制 Nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
