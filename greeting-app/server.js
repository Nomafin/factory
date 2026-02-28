const http = require("http");

const PORT = process.env.PORT || 3000;
const GREETING = "Hur mår du, Linköping!?!?";

const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
    return;
  }

  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(`<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Greeting App</title>
  <style>
    body { font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f0f4f8; }
    h1 { font-size: 2.5rem; color: #1a202c; }
  </style>
</head>
<body>
  <h1>${GREETING}</h1>
</body>
</html>`);
});

server.listen(PORT, () => {
  console.log(`Greeting app listening on port ${PORT}`);
});
