const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Health check endpoint
app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

// API endpoint to generate greeting
app.post('/api/greet', (req, res) => {
  const { name } = req.body;
  if (!name || name.trim() === '') {
    return res.status(400).json({ error: 'Name is required' });
  }
  res.json({ greeting: `Hello, ${name.trim()}! Welcome!` });
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Greeting app running on http://0.0.0.0:${PORT}`);
});
