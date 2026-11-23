(() => {
  const own = key => Object.hasOwn(sqlPulseMessages, key);
  const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const keys = Object.keys(sqlPulseMessages).sort((a, b) => b.length - a.length);
  const matches = new RegExp(keys.map(escape).join('|'), 'g');
  globalThis.sqlPulseUiText = value => {
    if (typeof value !== 'string' || !/[\u3400-\u9fff]/.test(value)) return value;
    if (own(value)) return sqlPulseMessages[value];
    return value.replace(matches, key => sqlPulseMessages[key]).trim();
  };
})();
