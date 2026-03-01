FROM node:20-alpine

WORKDIR /app

COPY package.json ./
COPY server.js ./

RUN apk add --no-cache curl

EXPOSE 3000

CMD ["node", "server.js"]
