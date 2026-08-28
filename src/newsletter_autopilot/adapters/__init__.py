"""Platform adapters. The pipeline talks to this interface and nothing else."""

from .base import BaseAdapter, PublisherAdapter
from .mock import MockPublisher

__all__ = ["BaseAdapter", "MockPublisher", "PublisherAdapter"]
