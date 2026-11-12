# DeepSeek V4 tokenizer

This directory pins the official DeepSeek V4 tokenizer used for exact request
budgeting.

- Source: <https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip>
- Documentation: <https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/>
- ZIP SHA-256: `e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495`
- `deepseek-v4-tokenizer.json` SHA-256:
  `89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614`

The runtime verifies the JSON digest before loading it. If the file is missing
or changed, model budgeting fails closed rather than falling back to character
counts or a guessed window.
