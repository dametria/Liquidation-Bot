# Indexer RPC limits

## Alchemy Free tier (Arbitrum)
`eth_getLogs` max block range = **10**

Set in `.env`:
```
INDEXER_CHUNK_SIZE=10
INDEXER_MIN_CHUNK=1
```

Pay-as-you-go / Growth: unlimited range on Arbitrum.

Public `arb1.arbitrum.io`: unreliable for getLogs (frequent 400s).
