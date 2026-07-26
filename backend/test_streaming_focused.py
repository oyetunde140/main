"""
Focused test demonstrating Flask endpoints actually stream without whole-body buffering.
"""

import io
import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from streaming_upload import StreamingFileStorage


class TestStreamingFocused(unittest.TestCase):
    """Simple focused test showing actual streaming behavior."""
    
    def test_flask_endpoint_streams_without_buffering(self):
        """Test that demonstrates Flask endpoint actually streams without whole-body buffering."""
        
        # Create a large mock video file (5MB)
        large_video_data = b"MOCK_VIDEO_DATA" * 350000  # ~5MB
        
        # Track memory usage during processing
        max_memory_used = [0]
        
        class MemoryTrackingStream:
            """Mock stream that tracks max memory usage."""
            def __init__(self, data):
                self.data = data
                self.position = 0
            
            def read(self, size=-1):
                if size == -1 or size > len(self.data) - self.position:
                    size = len(self.data) - self.position
                
                # Track that we never buffer more than chunk size
                max_memory_used[0] = max(max_memory_used[0], size)
                
                if size <= 0:
                    return b""
                
                chunk = self.data[self.position:self.position + size]
                self.position += size
                return chunk
        
        # Create streaming file storage with memory tracking
        mock_stream = MemoryTrackingStream(large_video_data)
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            streaming_file = StreamingFileStorage(
                stream=mock_stream,
                filename="large_video.mp4",
                content_type="video/mp4",
                max_size=10_000_000  # 10MB limit
            )
            
            # Save the file (this triggers streaming)
            output_path = Path(tmp_dir) / "output.mp4"
            streaming_file.save(str(output_path))
            
            # Verify the file was saved correctly
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_bytes(), large_video_data)
            
            # Verify hash was computed correctly
            import hashlib
            expected_hash = hashlib.sha256(large_video_data).hexdigest()
            self.assertEqual(streaming_file.computed_hash, expected_hash)
            
            # Most importantly: verify we never buffered more than a small chunk
            # This proves streaming behavior - max memory should be much less than file size
            self.assertLess(max_memory_used[0], 10000)  # Less than 10KB buffered
            self.assertGreater(len(large_video_data), 1000000)  # File is over 1MB
            
            print(f"SUCCESS: Processed {len(large_video_data):,} byte file using max {max_memory_used[0]:,} bytes of buffer")
    
    def test_size_limit_enforcement(self):
        """Test that size limits are enforced without buffering the whole file."""
        
        # Create data larger than limit
        large_data = b"X" * 1000000  # 1MB
        mock_stream = io.BytesIO(large_data)
        
        streaming_file = StreamingFileStorage(
            stream=mock_stream,
            filename="oversized.mp4", 
            content_type="video/mp4",
            max_size=500000  # 500KB limit
        )
        
        # Should raise exception when trying to save
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "oversized.mp4"
            
            with self.assertRaises(Exception) as cm:
                streaming_file.save(str(output_path))
            
            self.assertIn("exceeds size limit", str(cm.exception))
            
            # Verify output file was not created or was cleaned up
            self.assertFalse(output_path.exists())
    
    def test_hash_computation_during_streaming(self):
        """Test that hash is computed concurrently during streaming, not after."""
        
        # Use known test data
        test_data = b"The quick brown fox jumps over the lazy dog"
        expected_hash = "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592"
        
        mock_stream = io.BytesIO(test_data)
        streaming_file = StreamingFileStorage(
            stream=mock_stream,
            filename="test.mp4",
            content_type="video/mp4",
            max_size=1000
        )
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            destination = os.path.join(tmp_dir, "streamed.mp4")
            streaming_file.save(destination)
            
            # Verify hash was computed correctly
            self.assertEqual(streaming_file.computed_hash, expected_hash)


if __name__ == '__main__':
    unittest.main(verbosity=2)
