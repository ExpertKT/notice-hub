// 自动整理规则：后端规则 <-> 界面卡片模型的纯数据映射。
// 这个文件只做数据变换，**不碰 DOM**：渲染、读表单值都在 settings.js 里。
// 卡片上不展示的字段（priority、dedupe_key、后端将来加的扩展字段）原样挂在 card.raw 上，
// 保存时先铺回原对象再覆盖界面负责的字段，避免界面「没显示」就把用户数据抹掉。
(function () {
  var CATEGORIES = ['urgent', 'action', 'academic', 'info'];
  // 与 summarizer 的重要程度表一致：urgent=5、action=4、academic=3、info=3。界面上不暴露这个旋钮。
  var IMPORTANCE = { urgent: 5, action: 4, academic: 3, info: 3 };

  function asTextList(value) {
    if (value === null || value === undefined) return [];
    if (Array.isArray(value)) return value.map(function (item) { return String(item); });
    return [String(value)];
  }

  // 逗号分隔的输入框：中英文逗号、顿号、分号、换行都当分隔符，去空去首尾空白。
  function splitList(text) {
    return String(text === null || text === undefined ? '' : text)
      .split(/[,，、;；\n\r]+/)
      .map(function (item) { return item.trim(); })
      .filter(function (item) { return item.length > 0; });
  }

  function payloadOf(rule) {
    var action = rule && rule.action;
    if (!action || typeof action !== 'object' || Array.isArray(action)) return {};
    var payload = action.payload;
    return payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : {};
  }

  function categoryOf(rule) {
    var category = payloadOf(rule).category;
    return CATEGORIES.indexOf(category) >= 0 ? category : 'action';
  }

  function rulesToCards(rules) {
    var list = Array.isArray(rules) ? rules : [];
    return list.map(function (rule) {
      var source = rule && typeof rule === 'object' ? rule : {};
      var match = source.match && typeof source.match === 'object' ? source.match : {};
      var scope = source.scope;
      var scoped = Array.isArray(scope) && scope.length > 0;
      var regexes = asTextList(match.regex);
      var summary = payloadOf(source).summary;
      return {
        id: String(source.id === null || source.id === undefined ? '' : source.id),
        enabled: source.enabled !== false,
        scopeMode: scoped ? 'groups' : 'all',
        scopeText: scoped ? scope.map(function (item) { return String(item); }).join(', ') : '',
        keywordText: asTextList(match.keyword).join(', '),
        atMe: match.at_me === true,
        useRegex: regexes.length > 0,
        regexText: regexes.join(', '),
        senderText: asTextList(match.sender).join(', '),
        summary: String(summary === null || summary === undefined ? '' : summary),
        category: categoryOf(source),
        raw: source
      };
    });
  }

  function cardsToRules(cards) {
    var list = Array.isArray(cards) ? cards : [];
    var rules = [];
    for (var index = 0; index < list.length; index += 1) {
      var card = list[index] && typeof list[index] === 'object' ? list[index] : {};
      var number = index + 1;
      var keyword = splitList(card.keywordText);
      var regex = card.useRegex ? splitList(card.regexText) : [];
      var sender = splitList(card.senderText);
      var atMe = card.atMe === true;
      if (!keyword.length && !regex.length && !sender.length && !atMe) {
        return {
          ok: false,
          error: '第 ' + number + ' 条规则缺少匹配条件：关键词、正则、指定发件人、只处理 @我，至少填一项。'
        };
      }
      var groups = splitList(card.scopeText);
      if (card.scopeMode === 'groups' && !groups.length) {
        return { ok: false, error: '第 ' + number + ' 条规则选择了「只处理指定群」，但群 ID 是空的。' };
      }
      var raw = card.raw && typeof card.raw === 'object' ? card.raw : {};
      var rawAction = raw.action;
      if (typeof rawAction === 'string') rawAction = { action: rawAction };
      if (!rawAction || typeof rawAction !== 'object' || Array.isArray(rawAction)) rawAction = {};
      var payload = {};
      var rawPayload = rawAction.payload;
      if (rawPayload && typeof rawPayload === 'object' && !Array.isArray(rawPayload)) {
        Object.keys(rawPayload).forEach(function (key) { payload[key] = rawPayload[key]; });
      }
      var category = CATEGORIES.indexOf(card.category) >= 0 ? card.category : 'action';
      payload.summary = String(card.summary === null || card.summary === undefined ? '' : card.summary).trim();
      payload.category = category;
      payload.importance = IMPORTANCE[category] || 3;

      var match = {};
      if (keyword.length) match.keyword = keyword;
      if (regex.length) match.regex = regex;
      if (atMe) match.at_me = true;
      if (sender.length) match.sender = sender;

      var rule = {};
      // 先原样铺回后端给的字段（含 priority / dedupe_key / 未知扩展），再覆盖界面负责的部分。
      Object.keys(raw).forEach(function (key) { rule[key] = raw[key]; });
      rule.id = String(card.id === null || card.id === undefined ? '' : card.id).trim() || ('规则 ' + number);
      rule.enabled = card.enabled !== false;
      rule.scope = card.scopeMode === 'groups' ? groups : '*';
      rule.match = match;
      var action = { action: rawAction.action || 'promote', payload: payload };
      Object.keys(rawAction).forEach(function (key) {
        if (key !== 'action' && key !== 'payload') action[key] = rawAction[key];
      });
      rule.action = action;
      rule.priority = typeof raw.priority === 'number' && isFinite(raw.priority) ? raw.priority : 0;
      rules.push(rule);
    }
    return { ok: true, rules: rules };
  }

  window.NhLabRules = { rulesToCards: rulesToCards, cardsToRules: cardsToRules };
})();
