    """Load persisted datasets from storage directory."""
    if not os.path.exists(config.storage_dir):
        return

    try:
        for filename in os.listdir(config.storage_dir):
            if filename.endswith('.json'):
                filepath = os.path.join(config.storage_dir, filename)
                try:
                    with open(filepath, 'r') as f:
                        data = json.load(f)
                    dataset_id = filename[:-5]  # Remove .json extension
                    # Validate the data structure
                    if 'columns' in data and 'rows' in data:
                        datasets[dataset_id] = data
                except (json.JSONDecodeError, KeyError):
                    # Skip invalid files
                    continue
    except OSError:
        pass


def _save_datasets_to_storage():
    """Save all datasets to storage directory."""
    try:
        os.makedirs(config.storage_dir, exist_ok=True)
        for dataset_id, data in datasets.items():
            filepath = _get_dataset_storage_path(dataset_id)
            try:
                with open(filepath, 'w') as f:
                    json.dump(data, f)
            except (OSError, TypeError):
                # Skip datasets that can't be serialized
                continue
    except OSError:
        pass


if __name__ == '__main__':
    main()
