# Harpocrates

Harpocrates is a Stellar Testnet evidence protocol for sensitive video. It registers video integrity, portable proof metadata, and one of three identity tiers:

- Silent Witness: anonymous credential proof with nullifier replay protection.
- Consistent Source: pseudonymous Stellar wallet source.
- Public Seal: verified institutional issuer.

## Workspace

```text
frontend/   React app for Evidence Studio and Verification Portal
backend/    Flask video metadata service
contracts/  Soroban contract workspace
project.md  Product and architecture spec
DESIGN.md   Visual design source
```

## Quick Start

### 1. Backend

```powershell
cd backend
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
python app.py
```

Backend runs at `http://127.0.0.1:5050`.

### 2. Frontend

```powershell
cd frontend
npm install
npm run dev
```

Frontend runs at `http://localhost:5173`.

### 3. Contracts

```powershell
cd contracts
cargo test
stellar contract build
```

## Stellar Testnet Deployment

Create or select a Stellar identity:

```powershell
stellar keys generate harpocrates-admin --network testnet --fund
```

Build and deploy:

```powershell
cd contracts
stellar contract build
stellar contract deploy `
  --wasm target\\wasm32-unknown-unknown\\release\\harpocrates_registry.wasm `
  --source harpocrates-admin `
  --network testnet
```

After deployment, put the contract ID in `frontend/.env.local`:

```text
VITE_HARPOCRATES_REGISTRY_ID=YOUR_CONTRACT_ID
VITE_API_BASE=http://127.0.0.1:5050
VITE_STELLAR_RPC_URL=https://soroban-testnet.stellar.org
VITE_STELLAR_READONLY_SOURCE=YOUR_FUNDED_TESTNET_ACCOUNT
```

Current Testnet deployment:

```text
HarpocratesRegistry=CCKTQNMBLXZXMWVR2WG4HDDUI3QGJU5LV5NTLFPCB72UITWE5TEDK7BT
SilentWitnessUltraHonkVerifier=CCP2EQPKT5XAYTOARX3LGHNMJ37A6W2WY3H54MRIHEZVTVAZZPUSGZQJ
Admin=GDVRSXIO4SK2KSMUKJTQHMDDHBBFC7NGZZ6WLVOPKAG47GYPYAZCZR7G
Issuer=GAJ3GSKWOCTI2B3ZQRTB7TWYYIX734VJLNGOMSIQSAGRMIGX6SSVMIB4
RegistryWasmHash=a0e9bd49967bae7cb3608166b323514c8ef46832905f56b3c4a5aff41e105e68
```

Live check:

```text
register_anonymous_verified with active credential root succeeded on Testnet.
Last E2E tx=c2d02db6cb2453650987cfb475b21184c558c5437cdef2bd02969f39db021616
```

## Current Implementation Status

Done:

- React Evidence Studio and Verification Portal shell.
- Local SHA-256 video hashing.
- Three identity tier UI.
- Flask video steganography service with border encoding and LSB fallback.
- NeonDB proof event storage.
- Registration event persistence after Stellar submission.
- Verification portal lookup across embedded metadata, NeonDB, and Soroban `get_by_video`.
- Real Noir Silent Witness circuit with UltraHonk proof generation and local verification.
- Browser-side Noir JS and bb.js prover for user-seeded, video-specific Silent Witness proofs.
- Soroban registry with all three identity paths.
- Soroban registry hook for a dedicated Noir UltraHonk verifier contract.
- Credential-root allowlist/revocation, issuer registry, and nullifier protection.
- Contract tests for all tiers.
- One-command E2E smoke test for steganography, browser-compatible Noir proving, NeonDB, and optional Stellar Testnet registration.
- Typed frontend registry client and manual production chunking for Stellar, Noir, React, and proof runtime bundles.
- Backend production hardening: security headers, readiness checks, stricter upload/metadata validation, safer CORS config, and production-gated local Noir worker.
- Typed Soroban contract events with `#[contractevent]` for proof, issuer, verifier, and credential-root lifecycle events.

Next:

- Hosted deployment packaging for frontend/backend environments.

## E2E Smoke Test

Run the local E2E path:

```powershell
.\scripts\e2e-harpocrates.ps1
```

This creates a synthetic video, embeds Harpocrates metadata, extracts it back,
generates a browser-compatible Noir UltraHonk proof, writes a NeonDB registration
event, and checks lookup by the embedded video hash.

Submit the same flow to Stellar Testnet:

```powershell
.\scripts\e2e-harpocrates.ps1 -RegisterOnChain
```

The on-chain mode calls `register_anonymous_verified`, verifies the returned
contract record matches the embedded video hash, then stores the transaction
result in NeonDB.

For a fresh registry that enforces active Silent Witness credentials, use:

```powershell
.\scripts\e2e-harpocrates.ps1 -RegisterOnChain -EnsureCredentialRoot
```

Deploy a fresh Testnet registry and run E2E:

```powershell
.\scripts\deploy-testnet.ps1
```

## Noir Proofs

Tier 1 now has a real Noir circuit in `zk/noir/silent_witness`.

Run from PowerShell:

```powershell
.\zk\noir\scripts\build-silent-witness-wsl.ps1
```

Current verified local toolchain:

```text
nargo 1.0.0-beta.9
bb 0.87.0
UltraHonk proof verified successfully
```

Export proof artifacts as hex:

```powershell
.\zk\noir\scripts\export-silent-witness-artifacts.ps1
```

Generate a video-specific proof with caller-provided field secrets:

```powershell
.\zk\noir\scripts\generate-silent-witness-wsl.ps1 `
  -VideoHash YOUR_32_BYTE_HEX `
  -CredentialSecret YOUR_FIELD_DECIMAL `
  -NullifierSecret YOUR_FIELD_DECIMAL
```

The registry now exposes `register_anonymous_verified` and can call a separate
`verify_proof(public_inputs, proof)` contract. It also requires the public
`credential_root` from the Noir proof to be active in the registry.

The frontend uses the same Noir artifacts from `frontend/public/noir` to produce
Silent Witness proofs in the browser. To smoke-test that path in Node:

```powershell
cd frontend
node scripts/test-noir-client-prover.mjs
```

## Backend API

```text
GET  /health
POST /api/stego/embed
POST /api/stego/extract
POST /api/noir/silent-witness
GET  /api/proofs?limit=25&cursor=<opaque>
GET  /api/proofs/by-video/:video_hash
POST /api/proofs/register
```

`GET /api/proofs` uses keyset (cursor) pagination so pages stay stable as new
evidence events arrive:

- `limit` — page size (default `25`, clamped to `1..100`)
- `cursor` — optional opaque token from a previous `nextCursor`
- Ordering — `id DESC` (primary key is the unique tie-breaker)
- Malformed `cursor` values return HTTP `400` with `{"error":"invalid cursor"}`

Response shape:

```json
{
  "ok": true,
  "events": [ /* up to `limit` proof_events rows */ ],
  "nextCursor": "opaque-token-or-null"
}
```

When `nextCursor` is non-null, pass it as `cursor` on the next request to
continue. When it is `null`, there are no further pages.

`/api/stego/embed` returns an embedded `video/mp4` artifact. The response
headers include:

```text
X-Harpocrates-Source-Hash
X-Harpocrates-Embedded-Hash
X-Harpocrates-Metadata-Hash
```

The frontend registers `X-Harpocrates-Embedded-Hash` on Stellar, so verifiers
hash the received embedded video and look that hash up on-chain.

`/api/noir/silent-witness` remains available as a local developer prover for a
specific `videoHash`. The product flow uses browser-side Noir JS and bb.js
instead, so user private seeds are not sent to the Flask service.

`/api/proofs/register` stores the Stellar transaction result in NeonDB:

```json
{
  "fileName": "evidence.mp4",
  "videoHash": "32-byte hex",
  "metadataHash": "32-byte hex",
  "proofId": "32-byte hex",
  "tier": "source",
  "txHash": "stellar transaction hash",
  "txStatus": "PENDING",
  "sourceAddress": "G...",
  "contractId": "C..."
}
```

## Contract Scripts

PowerShell helpers live in `contracts/scripts`.

```powershell
.\contracts\scripts\add-issuer.ps1 `
  -ContractId CC2RBQ4GRGWNXMIW5OUL3HGQ5LCNUK7DCMZ76EEFZCPCXMTAGQ33Y5MY `
  -Admin harpocrates-admin `
  -Issuer harpocrates-issuer `
  -MetadataHash YOUR_32_BYTE_HEX

.\contracts\scripts\register-seal.ps1 `
  -ContractId CC2RBQ4GRGWNXMIW5OUL3HGQ5LCNUK7DCMZ76EEFZCPCXMTAGQ33Y5MY `
  -Issuer harpocrates-issuer `
  -VideoHash YOUR_32_BYTE_HEX `
  -MetadataHash YOUR_32_BYTE_HEX `
  -ProofId YOUR_32_BYTE_HEX

.\contracts\scripts\set-verifier.ps1 `
  -ContractId YOUR_REGISTRY_CONTRACT_ID `
  -Admin harpocrates-admin `
  -Verifier YOUR_VERIFIER_CONTRACT_ID

.\contracts\scripts\add-credential-root.ps1 `
  -ContractId YOUR_REGISTRY_CONTRACT_ID `
  -Admin harpocrates-admin `
  -CredentialRoot YOUR_32_BYTE_HEX `
  -MetadataHash YOUR_32_BYTE_HEX

.\contracts\scripts\revoke-credential-root.ps1 `
  -ContractId YOUR_REGISTRY_CONTRACT_ID `
  -Admin harpocrates-admin `
  -CredentialRoot YOUR_32_BYTE_HEX
```
