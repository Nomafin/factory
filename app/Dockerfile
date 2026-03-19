FROM node:20-alpine

WORKDIR /app

# Install curl for health checks
RUN apk add --no-cache curl

COPY package.json ./
RUN npm install --production

COPY . .

EXPOSE 3000

CMD ["node", "server.js"]
