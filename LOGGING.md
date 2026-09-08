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
   - **INFO**   → marco caro e raro; NÃO happy-path por request — com **uma
                  excepção declarada**, o `outbound_markup` da nota abaixo.
   - **DEBUG**  → trace de fidelidade total (payloads). DEV-ONLY, jamais ligado
                  em produção multi-tenant. Redija secrets (apikey).
4. Controle de nível é por pacote: `logging.getLogger("cogno_gateway").setLevel(...)`.

O host anexa o handler (TenantFilter + JsonFormatter) ao root logger real;
veja `cogno/core/logging.py` no host como referência.

## Nota específica do cogno-gateway

- **WARNING** em `verify()` falho (assinatura/secret/apikey inválida) e em
  `HTTPError` no send (`SendResult.ok=False`).
- **WARNING também num send que RECUPEROU**, e é a excepção à linha acima: o
  `event=send_retry attempt=2` do Telegram dispara quando a 1.ª chamada HTTP falhou
  por transporte, e a chamada seguinte pode ter sucesso — `SendResult.ok=True`. Não é
  um caminho feliz a gritar: é o único registo de que aquela resposta pode agora
  existir **duas vezes** no telemóvel do contacto, e sem esta linha não há como
  contá-lo depois. O `after=` traz a CLASSE da excepção e nunca só o `str(exc)`, que
  num `ReadTimeout` é vazio e faria a linha dizer `error=` — indistinguível de «não
  houve erro».
- **INFO em `event=outbound_markup`, e é a segunda excepção declarada** — esta à
  regra «INFO não é happy-path por request». A linha dispara em **todos** os
  envios e mesmo assim tem de ser INFO: a conversão de negrito por canal acontece
  **dentro** do `send`, e **nada persiste o payload já convertido**. O host grava
  a resposta ANTES do adaptador e mede o seu `event=outbound_attempted chars=` na
  linha acima do `channel.send(...)`, portanto a pergunta «o `**` ainda chega ao
  contacto?» não tinha coluna nenhuma que a respondesse — em nenhum sentido. Em
  DEBUG ficaria atrás de um interruptor que em produção está desligado, que é o
  mesmo que não existir. O custo está medido e é limitado: **uma linha por
  mensagem enviada, não por chunk** (a conversão corre antes do chunker).
- **O `outbound_markup` nunca leva o texto** — e isso é a decisão, não uma
  omissão. A resposta de saída é o dado do contacto: nome, número, os valores que
  uma ferramenta leu. Um log que o incluísse abriria um depósito novo de dados
  pessoais para fechar um buraco de observabilidade, que é a troca que o allowlist
  de PII de saída já recusa uma camada acima (guarda digests, nunca valores).
  `chars_in`/`chars_out` respondem ao que foi perguntado — correu, e mudou alguma
  coisa? — sem um byte da mensagem.
- O `chars_in` deve bater com o `chars=` do `outbound_attempted` do host **no
  mesmo turno**: é a junção que fecha o par pré-adaptador/pós-adaptador, e uma
  divergência diz que alguma coisa mexeu no texto entre as duas.
- O `channel=` desta linha é o rótulo do próprio módulo, como em todas as outras:
  `whatsapp` (Evolution) e `whatsapp_cloud` (Cloud API) partilham a célula
  `whatsapp` da tabela de markup, e o rótulo é o que os distingue. Qual dialecto
  correu lê-se melhor no par de números do que na tabela: a troca perde um
  carácter por delimitador, a remoção perde dois.
- Parse/send de happy-path é **DEBUG** (o host é dono do ciclo de request).
- O **payload bruto** do webhook vai em **DEBUG** (dev-only) e a **`apikey` da
  Evolution é redigida mesmo em DEBUG** — secret ≠ conteúdo de usuário; vazar
  credencial em log de dev ainda é inaceitável.
