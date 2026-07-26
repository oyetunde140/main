# Streaming Upload Implementation

This implements streaming multipart uploads for large video files without buffering entire payloads in memory.

## Implementation

The solution adds a `StreamingFileStorage` class that:

1. **Streams to disk**: Writes uploaded data directly to temporary files using 8KB chunks
2. **Concurrent hashing**: Computes SHA-256 hash during streaming, not after
3. **Size limit enforcement**: Checks limits during streaming and aborts early if exceeded
4. **Automatic cleanup**: Removes temporary files on success or failure

## Integration

Large uploads (>250MB by default) automatically use streaming via `_enable_streaming_for_large_uploads()` in the embed/extract endpoints. Smaller uploads continue using the standard Flask multipart parser for compatibility.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `UPLOAD_MAX_BYTES` | 262,144,000 (250MB) | Size threshold for streaming |
| `UPLOAD_TEMP_DIR` | OS temp directory | Temporary file location |
| `UPLOAD_TIMEOUT_SECONDS` | 300 | Upload timeout |
| `UPLOAD_MAX_CONCURRENT` | 10 | Concurrent upload limit |

## Testing

The focused test `test_streaming_focused.py` demonstrates:
- Processing 5MB+ files using only 8KB of buffer memory
- Size limit enforcement without full buffering  
- Hash computation during streaming

## Memory Usage

**Before**: 250MB+ RAM per large upload (entire file buffered)  
**After**: ~8KB RAM per upload (streaming chunks only)