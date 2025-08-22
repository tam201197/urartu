from aim import Run
from omegaconf import DictConfig
from typing import Dict, Any, Optional
from abc import ABC, abstractmethod
from pathlib import Path
import hashlib
import json
import pickle
import time
import logging
import gc
import sys
from datetime import datetime

from .device import Device
from urartu.utils.hash import dict_to_8char_hash

logger = logging.getLogger(__name__)


class Action(ABC):
    """
    A class to manage and configure actions based on a configuration and an Aim run session.

    This class initializes an action with specific configurations for task execution,
    sets up device configuration for the action, and ties the action to an Aim run session
    for tracking and logging purposes.

    Attributes:
        cfg (DictConfig): The full configuration object, typically containing all settings.
        action_config (DictConfig): Configuration specific to the action.
        aim_run (Run): An Aim run instance for tracking and logging the execution of the action.
    """

    def __init__(self, cfg: DictConfig, aim_run: Run):
        """
        Initializes the Action object with the necessary configuration and Aim session.

        Args:
            cfg (DictConfig): The configuration object providing settings for various components of the action.
            aim_run (Run): The Aim run session to track and manage execution metrics and logs.

        Sets up the device configuration for the action by calling an external method set_device
        from the Device class with the device setting specified in the action configuration.
        """
        self.cfg = cfg
        # Handle the case where cfg.action_config might be the action name (string) or config (dict)
        action_attr = getattr(cfg, 'action_config', {})
        if isinstance(action_attr, str):
            # cfg.action_config is the action name, look for action config elsewhere
            # For pipeline actions, use the pipeline config
            if hasattr(cfg, 'pipeline_config') and cfg.pipeline_config:
                self.action_config = cfg.pipeline_config
            else:
                # For individual actions, the config is at the top level (flattened)
                self.action_config = cfg
        else:
            # cfg.action_config is the action config dictionary
            self.action_config = action_attr
        
        self.aim_run = aim_run
        # Handle both regular dict and OmegaConf DictConfig
        has_device_config = (isinstance(self.action_config, dict) or 
                           (hasattr(self.action_config, 'get') and hasattr(self.action_config, 'keys')))
        device = self.action_config.get('device', 'auto') if has_device_config else 'auto'
        Device.set_device(device)
        
        # Set up caching system
        if has_device_config:  # Use the same check as device config
            self.cache_enabled = self.action_config.get('cache_enabled', True)
            self.force_rerun = self.action_config.get('force_rerun', False) 
            self.cache_max_age = self.action_config.get('cache_max_age_hours', None)
        else:
            # Fallback defaults when action_config is not a dict-like object
            self.cache_enabled = True
            self.force_rerun = False
            self.cache_max_age = None
            
        if self.cache_max_age is not None:
            self.cache_max_age = self.cache_max_age * 3600  # Convert to seconds
        
        # Use a shared cache directory by extracting the base .runs folder from run_dir
        # run_dir is typically something like: /path/to/.runs/pipeline_name/timestamp/
        # We want to use: /path/to/.runs/action_cache/
        run_dir_path = Path(self.cfg.get('run_dir', '.'))
        
        # Find the .runs directory in the path hierarchy
        runs_dir = None
        for parent in [run_dir_path] + list(run_dir_path.parents):
            if parent.name == '.runs':
                runs_dir = parent
                break
        
        # Fallback: assume run_dir is inside .runs and go up until we find it
        if runs_dir is None:
            current_path = run_dir_path
            while current_path != current_path.parent:  # Stop at filesystem root
                if current_path.name == '.runs' or (current_path / '.runs').exists():
                    runs_dir = current_path if current_path.name == '.runs' else current_path / '.runs'
                    break
                current_path = current_path.parent
        
        # Final fallback: use current directory
        if runs_dir is None:
            runs_dir = Path('.') / '.runs'
        
        self.cache_dir = runs_dir / 'action_cache'
        self._cached_outputs = None
        self._cache_key = None
    
    def get_outputs(self) -> Dict[str, Any]:
        """
        Return the outputs produced by this action.
        
        This method should be overridden by subclasses to return relevant outputs
        that can be used by subsequent actions in a pipeline.
        
        Returns:
            Dict[str, Any]: A dictionary of output keys and values.
        """
        return {}
    
    def _generate_cache_key(self) -> str:
        """Generate a unique cache key based on action configuration."""
        # Include action name and full configuration in cache key
        key_factors = {
            'action_name': self.__class__.__name__,
            'config': self._get_serializable_config()
        }
        
        # Convert to JSON for consistent ordering
        key_string = json.dumps(key_factors, sort_keys=True)
        
        # Generate hash
        cache_key = hashlib.sha256(key_string.encode()).hexdigest()[:16]
        
        return f"{self.__class__.__name__}_{cache_key}"
    
    def _get_serializable_config(self) -> Dict[str, Any]:
        """Get a serializable version of the configuration for caching."""
        try:
            from omegaconf import OmegaConf
            if hasattr(self, 'action_config'):
                return OmegaConf.to_container(self.action_config, resolve=True)
            return {}
        except Exception:
            # Fallback to dict conversion
            if hasattr(self, 'action_config') and hasattr(self.action_config, 'items'):
                return dict(self.action_config)
            return {}
    
    def _get_cache_path(self, cache_key: str) -> Path:
        """Get the file path for a cache entry."""
        return self.cache_dir / f"{cache_key}.pkl"
    
    def _load_from_cache(self) -> Optional[Dict[str, Any]]:
        """Load outputs from cache if available and valid."""
        if not self.cache_enabled or self.force_rerun:
            return None
        
        cache_key = self._generate_cache_key()
        cache_path = self._get_cache_path(cache_key)
        
        if not cache_path.exists():
            return None
        
        try:
            with open(cache_path, 'rb') as f:
                cache_data = pickle.load(f)
            
            # Check if cache is still valid
            if self.cache_max_age is not None:
                age = time.time() - cache_data['timestamp']
                if age > self.cache_max_age:
                    logger.info(f"Cache for {self.__class__.__name__} is expired (age: {age:.1f}s)")
                    return None
            
            logger.info(f"Loading cached outputs for {self.__class__.__name__}")
            self.aim_run[f"action_{self.__class__.__name__}_cache_hit"] = True
            return cache_data['outputs']
            
        except Exception as e:
            logger.warning(f"Failed to load cache for {self.__class__.__name__}: {e}")
            return None
    
    def _save_to_cache(self, outputs: Dict[str, Any]):
        """Save outputs to cache."""
        if not self.cache_enabled:
            return
        
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            
            cache_key = self._generate_cache_key()
            cache_path = self._get_cache_path(cache_key)
            
            cache_data = {
                'outputs': outputs,
                'timestamp': time.time(),
                'action_name': self.__class__.__name__,
                'config_hash': hashlib.sha256(json.dumps(self._get_serializable_config(), sort_keys=True).encode()).hexdigest()[:8]
            }
            
            with open(cache_path, 'wb') as f:
                pickle.dump(cache_data, f)
            
            # Also save human-readable metadata
            metadata_path = cache_path.with_suffix('.json')
            metadata = {
                'cache_key': cache_key,
                'action_name': self.__class__.__name__,
                'timestamp': datetime.fromtimestamp(cache_data['timestamp']).isoformat(),
                'config_hash': cache_data['config_hash'],
                'output_keys': list(outputs.keys())
            }
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            logger.info(f"Cached outputs for {self.__class__.__name__} with key {cache_key}")
            self.aim_run[f"action_{self.__class__.__name__}_cache_hit"] = False
            
        except Exception as e:
            logger.warning(f"Failed to save cache for {self.__class__.__name__}: {e}")
    
    def run_with_cache(self):
        """
        Run the action with caching support.
        
        This method should be called instead of run() directly to enable caching.
        It will check cache first, run the action if needed, and save to cache.
        """
        # Try to load from cache first
        cached_outputs = self._load_from_cache()
        if cached_outputs is not None:
            self._cached_outputs = cached_outputs
            return
        
        # Cache miss - run the action
        logger.info(f"Running {self.__class__.__name__} (cache miss)")
        
        # Call the actual run method (must be implemented by subclasses)
        if hasattr(self, 'run'):
            self.run()
        elif hasattr(self, 'main'):
            self.main()
        else:
            raise NotImplementedError(f"Action {self.__class__.__name__} must implement run() or main() method")
        
        # Get outputs and save to cache
        outputs = self.get_outputs()
        if outputs:
            self._save_to_cache(outputs)
            self._cached_outputs = outputs
    
    def clear_cache(self):
        """Clear the cache for this action."""
        if self.cache_dir.exists():
            cache_key = self._generate_cache_key()
            cache_path = self._get_cache_path(cache_key)
            
            if cache_path.exists():
                cache_path.unlink()
                metadata_path = cache_path.with_suffix('.json')
                if metadata_path.exists():
                    metadata_path.unlink()
                logger.info(f"Cleared cache for {self.__class__.__name__}")
    
    def cleanup_memory(self):
        """
        Clean up memory resources used by this action.
        
        This method should be called after an action completes to free up GPU and RAM
        memory for subsequent actions in a pipeline. It performs generic cleanup and
        calls action-specific cleanup hooks.
        """
        logger.info(f"🧹 Cleaning up memory for action: {self.__class__.__name__}")
        
        # Get initial memory stats
        initial_gpu_memory = self._get_gpu_memory_mb()
        initial_ram_gb = self._get_ram_usage_gb()
        
        # Call action-specific cleanup first
        try:
            self._action_specific_cleanup()
        except Exception as e:
            logger.warning(f"Action-specific cleanup failed: {e}")
        
        # Generic cleanup of common ML objects
        self._cleanup_common_attributes()
        
        # Clear GPU memory cache
        self._clear_gpu_cache()
        
        # Force garbage collection
        collected = gc.collect()
        logger.info(f"Garbage collector freed {collected} objects")
        
        # Log memory savings
        final_gpu_memory = self._get_gpu_memory_mb()
        final_ram_gb = self._get_ram_usage_gb()
        
        gpu_freed = initial_gpu_memory - final_gpu_memory if initial_gpu_memory and final_gpu_memory else None
        ram_freed = initial_ram_gb - final_ram_gb if initial_ram_gb and final_ram_gb else None
        
        if gpu_freed and gpu_freed > 0:
            logger.info(f"✅ Freed {gpu_freed:.1f} MB of GPU memory")
        if ram_freed and ram_freed > 0.1:  # Only log if significant
            logger.info(f"✅ Freed {ram_freed:.2f} GB of RAM")
        
        logger.info(f"🧹 Memory cleanup completed for {self.__class__.__name__}")
    
    def _action_specific_cleanup(self):
        """
        Hook for action subclasses to implement custom memory cleanup.
        
        Override this method in subclasses to clean up action-specific resources
        like models, datasets, or other large objects.
        """
        pass
    
    def _cleanup_common_attributes(self):
        """Clean up commonly used attributes in ML actions."""
        # List of common attribute names that might hold large objects
        common_large_attributes = [
            'model', '_model', 'tokenizer', '_tokenizer',
            'dataset', '_dataset', 'raw_dataset', 'train_dataset', 'test_dataset',
            'dataloader', 'train_dataloader', 'test_dataloader',
            'optimizer', '_optimizer', 'scheduler', '_scheduler',
            'criterion', '_criterion', 'loss_fn',
            'embeddings', '_embeddings', 'features', '_features',
            'predictions', '_predictions', 'logits', '_logits',
            'cache', '_cache', 'memory_cache'
        ]
        
        cleaned_attributes = []
        for attr_name in common_large_attributes:
            if hasattr(self, attr_name):
                attr_value = getattr(self, attr_name)
                if attr_value is not None:
                    # For models/torch objects, try to move to CPU first
                    try:
                        if hasattr(attr_value, 'cpu'):
                            attr_value.cpu()
                        elif hasattr(attr_value, 'to'):
                            attr_value.to('cpu')
                    except:
                        pass  # Ignore errors during CPU movement
                    
                    # Set to None to free reference
                    setattr(self, attr_name, None)
                    cleaned_attributes.append(attr_name)
        
        if cleaned_attributes:
            logger.info(f"Cleaned up attributes: {', '.join(cleaned_attributes)}")
    
    def _clear_gpu_cache(self):
        """Clear GPU memory cache if available."""
        try:
            import torch
            if torch.cuda.is_available():
                initial_memory = torch.cuda.memory_allocated()
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
                final_memory = torch.cuda.memory_allocated()
                freed_mb = (initial_memory - final_memory) / (1024 * 1024)
                if freed_mb > 1:  # Only log if significant
                    logger.info(f"Cleared GPU cache, freed {freed_mb:.1f} MB")
        except ImportError:
            pass  # PyTorch not available
        except Exception as e:
            logger.warning(f"Failed to clear GPU cache: {e}")
    
    def _get_gpu_memory_mb(self) -> Optional[float]:
        """Get current GPU memory usage in MB."""
        try:
            import torch
            if torch.cuda.is_available():
                return torch.cuda.memory_allocated() / (1024 * 1024)
        except ImportError:
            pass
        return None
    
    def _get_ram_usage_gb(self) -> Optional[float]:
        """Get current RAM usage in GB."""
        try:
            import psutil
            process = psutil.Process()
            return process.memory_info().rss / (1024 * 1024 * 1024)
        except ImportError:
            pass
        return None

    def run(self):
        """
        Optional method that can be implemented by action subclasses.
        This method should contain the main logic of the action.
        
        If not implemented, the action should have a main() method instead.
        """
        raise NotImplementedError(f"Action {self.__class__.__name__} must implement either run() or main() method")


class ActionDataset(Action):
    """
    A specialized Action class for dataset-related operations.
    
    This class extends the base Action class with functionality specific to dataset handling.
    It automatically generates a unique hash for the dataset based on its name and configuration,
    and sets this information in the Aim run for tracking purposes.
    
    Attributes:
        Inherits all attributes from the Action class.
        
    Note:
        The dataset hash is created using the dataset name combined with an 8-character hash
        derived from the dataset configuration, ensuring uniqueness for tracking.
    """
    
    def __init__(self, cfg: DictConfig, aim_run: Run):
        """
        Initializes the ActionDataset object with the necessary configuration and Aim session.
        
        Args:
            cfg (DictConfig): The configuration object providing settings for various components of the action.
            aim_run (Run): The Aim run session to track and manage execution metrics and logs.
            
        Before initializing the parent Action class, this constructor:
        1. Generates a unique hash for the dataset configuration
        2. Adds this hash to the dataset configuration
        3. Sets the complete configuration in the Aim run
        """
        # Get the dataset config from the appropriate location
        dataset_config = None
        
        # Try different locations for dataset config based on configuration structure
        if hasattr(cfg, 'action_config') and cfg.action_config and hasattr(cfg.action_config, 'dataset'):
            # Pipeline actions: cfg.action_config.dataset
            dataset_config = cfg.action_config.dataset
        elif hasattr(cfg, 'pipeline_config') and cfg.pipeline_config and hasattr(cfg.pipeline_config, 'dataset'):
            # Pipeline actions fallback: cfg.pipeline_config.dataset
            dataset_config = cfg.pipeline_config.dataset
        elif hasattr(cfg, 'dataset'):
            # Individual actions: cfg.dataset
            dataset_config = cfg.dataset
        
        if dataset_config and hasattr(dataset_config, 'name'):
            dataset_config["hash"] = f"{dataset_config.name}_{dict_to_8char_hash(dataset_config)}"
        
        aim_run.set("cfg", cfg, strict=False)
        super().__init__(cfg, aim_run)
