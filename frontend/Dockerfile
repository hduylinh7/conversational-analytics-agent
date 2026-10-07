FROM node:22-alpine

WORKDIR /app

# Install dependencies first for efficient layer caching
COPY package*.json ./

RUN npm install

# Copy application source code
COPY . .

ENV PORT=3000
ENV NODE_ENV=development

EXPOSE 3000

CMD ["npm", "run", "dev"]
