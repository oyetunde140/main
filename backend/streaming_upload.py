from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional, Tuple

from flask import Flask, Request, g, request
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

from config import AppConfig
from logging_utils import log_structured


class StreamingFileStorage(FileStorage):
    """Simplified FileStorage that streams to disk while computing hash."""
    
    def __init__(self, stream: BinaryIO, filename: Optional[str] = None, 
                 content_type: Optional[str] = None, max_size: int = 0):
        super().__init__(stream, filename, content_type=content_type)
        self._max_size = max_size
        self._temp_file: Optional[BinaryIO] = None
        self._temp_path: Optional[Path] = None
        self._hasher = hashlib.sha256()
        self._bytes_written = 0
        self._hash_computed = False
    
    def save(self, dst: str) -> None:
        """Save the streaming content to destination while computing hash."""
        if self._temp_file is not None:
            # Already processed, just move the temp file
            self._temp_file.close()
            shutil.copyfile(self._temp_path, dst)
            self._temp_path.unlink(missing_ok=True)
            return
            
        # Create temp file and stream content
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            self._temp_path = Path(temp_file.name)
            
            # Stream data from source to temp file while hashing
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    break
                
                # Check size limit
                if self._max_size > 0 and self._bytes_written + len(chunk) > self._max_size:
                    temp_file.close()
                    self._temp_path.unlink(missing_ok=True)
                    raise RequestEntityTooLarge("Upload exceeds size limit")
                
                temp_file.write(chunk)
                self._hasher.update(chunk)
                self._bytes_written += len(chunk)
            
            self._temp_file = temp_file
        
        # Move to final destination
        shutil.copyfile(self._temp_path, dst)
        self._temp_path.unlink(missing_ok=True)
        self._hash_computed = True
    
    @property
    def computed_hash(self) -> Optional[str]:
        """Get the computed SHA-256 hash if available."""
        return self._hasher.hexdigest() if self._hash_computed else None
    
    def close(self) -> None:
        """Clean up temporary files."""
        super().close()
        if self._temp_path and self._temp_path.exists():
            self._temp_path.unlink(missing_ok=True)


def create_streaming_file_storage(field_storage) -> StreamingFileStorage:
    """Convert werkzeug FieldStorage to StreamingFileStorage."""
    config = g.get('upload_config')
    max_size = (
        getattr(config, "upload_max_bytes", config.max_video_bytes)
        if config
        else 0
    )
    
    return StreamingFileStorage(
        stream=field_storage.stream,
        filename=field_storage.filename,
        content_type=field_storage.content_type,
        max_size=max_size
    )
