FROM node:20-alpine
WORKDIR /web
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]
