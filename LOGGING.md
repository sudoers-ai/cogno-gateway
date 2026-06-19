# Logging — convenção desta lib

Esta biblioteca **emite** logs; o **host configura** (handlers, formato, nível,
contexto de tenant). Regras:

1. Use `logging.getLogger(__name__)` no topo do módulo. Nada de handlers,
   formatters, `basicConfig` ou um `get_logger` próprio.
2. Mensagem = só o fato de domínio, em `key=value`, sempre lazy:
   `logger.warning("channel=%s event=verify_failed reason=%s", ch, reason)`.
   NÃO coloque tenant_id / timestamp / channel na mensagem — o host injeta
   via contextvars + Filter no root logger (carimbado em todo LogRecord).
3. Níveis:
   - **ERROR**  → nunca aqui; erro fatal vira exceção e propaga (host loga ERROR).
   - **WARNING**→ condição recuperada/tratada (fallback, parse coercion, verify falho).
   - **INFO**   → marco caro e raro; NÃO happy-path por request.
   - **DEBUG**  → trace de fidelidade total (payloads). DEV-ONLY, jamais ligado
                  em produção multi-tenant. Redija secrets (apikey).
4. Controle de nível é por pacote: `logging.getLogger("cogno_gateway").setLevel(...)`.

O host anexa o handler (TenantFilter + JsonFormatter) ao root logger real;
veja `cogno/core/logging.py` no host como referência.

## Nota específica do cogno-gateway

- **WARNING** em `verify()` falho (assinatura/secret/apikey inválida) e em
  `HTTPError` no send (`SendResult.ok=False`).
- Parse/send de happy-path é **DEBUG** (o host é dono do ciclo de request).
- O **payload bruto** do webhook vai em **DEBUG** (dev-only) e a **`apikey` da
  Evolution é redigida mesmo em DEBUG** — secret ≠ conteúdo de usuário; vazar
  credencial em log de dev ainda é inaceitável.
