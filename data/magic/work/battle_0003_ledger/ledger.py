def post(self, desc: str, amount: float) -> None:
    if amount == 0.0:
        raise ValueError('Cannot post zero')
    self.entries.append((desc, amount))