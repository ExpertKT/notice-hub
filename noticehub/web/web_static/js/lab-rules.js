// 自动整理规则：后端规则 <-> 界面卡片模型的纯数据映射，外加关键词的标签式输入。
// 数据变换这部分**不碰 DOM**：渲染、读表单值都在 settings.js 里。
// 标签式输入（关键词一条一个标签）在文件末尾自动挂载到页面上的 input.lab-rule-keywords，
// 所以 settings.js / page.html 都不用改：底下那个 input 的 value 依旧是逗号串，契约不变。
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

  // ---- 关键词标签式输入 ----
  // 原来是一个文本框写「报名, 截止, 开会」：用户看不出这是多个值，全角逗号、顿号、空格各写一遍，
  // 少一个逗号就变成一整句话当关键词。改成一条一个标签后，输入框只负责「正在打的这个词」。
  // 数据契约不变：底下那个 input 的 value 依旧是逗号串，上面的 splitList 照旧吃它。
  var CHIP_SEPARATOR = /[,，、;；\n\r]+/;
  // 粘贴时把空格也算分隔符（别人的清单多是「a b c」）；自己手打时空格是词的一部分，不当分隔。
  var CHIP_PASTE_SEPARATOR = /[,，、;；\s]+/;

  function chipWords(text, splitSpaces) {
    var parts = String(text === null || text === undefined ? '' : text)
      .split(splitSpaces ? CHIP_PASTE_SEPARATOR : CHIP_SEPARATOR);
    var words = [];
    for (var index = 0; index < parts.length; index += 1) {
      var word = parts[index].trim();
      if (!word || words.indexOf(word) >= 0) continue;
      words.push(word);
    }
    return words;
  }

  // 加词：去空、去重（同一条规则里同一个词只留一个），已有顺序不变。
  function mergeChips(current, text, splitSpaces) {
    var list = Array.isArray(current) ? current.slice() : [];
    var words = chipWords(text, splitSpaces);
    for (var index = 0; index < words.length; index += 1) {
      if (list.indexOf(words[index]) < 0) list.push(words[index]);
    }
    return list;
  }

  function dropChip(current, index) {
    var list = Array.isArray(current) ? current.slice() : [];
    if (index >= 0 && index < list.length) list.splice(index, 1);
    return list;
  }

  var CHIP_STYLE_ID = 'nh-chips-style';
  // 只覆盖需要变的属性；底色/文字色全部用页面既有变量，深色主题自动跟随。
  // 标签和 × 都是 44px 高，手机上点得准，不会挤成一行小字。
  var CHIP_STYLE = [
    '.nh-chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;width:100%;max-width:420px;min-height:52px;padding:4px;border:1px solid var(--line,#b0b8bd);border-radius:6px;background:var(--paper,#fff);color:var(--ink,#172722)}',
    '.nh-chips:focus-within{border-color:#12695b;box-shadow:0 0 0 2px rgba(18,105,91,.14)}',
    '.nh-chip-list{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:0;padding:0;list-style:none}',
    '.nh-chip{display:inline-flex;align-items:center;max-width:100%;min-height:44px;padding:0 2px 0 12px;border:1px solid var(--line,#b0b8bd);border-radius:8px;background:var(--surface-2,#f2f4f6);font-size:13px;line-height:1.2}',
    '.nh-chip-text{overflow-wrap:anywhere;word-break:break-word}',
    '.nh-chips .nh-chip-x{flex:none;min-width:44px;min-height:44px;margin:0;padding:0;border:0;border-radius:6px;background:transparent;color:inherit;font:inherit;font-size:17px;line-height:1;cursor:pointer}',
    '.nh-chips .nh-chip-x:hover{background:var(--red-soft,#ffeeed);color:var(--red,#a5312d)}',
    '.nh-chips .nh-chip-x:focus-visible{outline:2px solid #12695b;outline-offset:-2px}',
    '.nh-chips>input{flex:1 1 8em;width:auto;min-width:6em;max-width:none;min-height:44px;margin:0;padding:0 8px;border:0!important;border-radius:0;background:transparent!important;box-shadow:none!important;color:inherit;font:inherit;font-size:13px;outline:none}'
  ].join('\n');

  function ensureChipStyles(doc) {
    if (!doc || !doc.createElement) return;
    if (doc.getElementById(CHIP_STYLE_ID)) return;
    var style = doc.createElement('style');
    style.id = CHIP_STYLE_ID;
    style.textContent = CHIP_STYLE;
    (doc.head || doc.documentElement).appendChild(style);
  }

  // 规则卡里还有两个「逗号分隔多值」字段，同一套实现直接挂上去。
  // 正则那个字段**故意不挂**：正则自己就带逗号（a{1,2}），按逗号拆会拆坏表达式。
  var CHIP_FIELDS = [
    { selector: 'input.lab-rule-keywords', noun: '关键词' },
    { selector: 'input.lab-rule-senders', noun: '发件人' },
    { selector: 'input.lab-rule-grouptext', noun: '群 ID' }
  ];

  // 把一个普通文本框原地升级成「标签 + 输入框」；已升级过的直接跳过。
  // settings.js 只做三件事：给这个 input 写 value、读 value、focus 它——三件都照旧能工作。
  function enhanceChips(input, noun) {
    if (!input || input.dataset.nhChips === '1') return;
    var doc = input.ownerDocument;
    if (!doc || !doc.createElement || !input.parentNode) return;
    ensureChipStyles(doc);

    var proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
    var nativeValue = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
    if (!nativeValue || !nativeValue.get || !nativeValue.set) return;
    function rawText() { return String(nativeValue.get.call(input) || ''); }
    function writeRaw(text) { nativeValue.set.call(input, text === null || text === undefined ? '' : String(text)); }

    input.dataset.nhChips = '1';
    noun = noun || '关键词';
    var words = chipWords(rawText(), false);
    writeRaw('');

    var wrap = doc.createElement('div');
    wrap.className = 'nh-chips';
    var list = doc.createElement('div');
    list.className = 'nh-chip-list';
    list.setAttribute('role', 'list');
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(list);
    wrap.appendChild(input);
    if (!input.placeholder || /[,，]/.test(input.placeholder)) {
      input.placeholder = noun === '关键词'
        ? '打一个词按回车；粘贴「报名, 截止」会拆成两个'
        : ('打一个' + noun + '按回车；粘贴一串用逗号分隔的会拆开');
    }

    // 让保存时拿到「已确认的标签 + 输入框里还没回车的半截词」，保存失败也不会丢内容。
    function committedText() {
      var all = words.slice();
      var tail = chipWords(rawText(), false);
      for (var index = 0; index < tail.length; index += 1) {
        if (all.indexOf(tail[index]) < 0) all.push(tail[index]);
      }
      return all.join(', ');
    }

    function notify() {
      if (typeof input.dispatchEvent !== 'function') return;
      var event;
      try { event = new window.Event('input', { bubbles: true }); } catch (error) { return; }
      input.dispatchEvent(event);   // settings.js 靠 input/change 记「这张卡有未保存的编辑」
    }

    function paint() {
      list.textContent = '';
      for (var index = 0; index < words.length; index += 1) {
        var chip = doc.createElement('span');
        chip.className = 'nh-chip';
        chip.setAttribute('role', 'listitem');
        var text = doc.createElement('span');
        text.className = 'nh-chip-text';
        text.textContent = words[index];
        var remove = doc.createElement('button');
        remove.type = 'button';
        remove.className = 'nh-chip-x';
        remove.textContent = '×';
        remove.dataset.index = String(index);
        remove.setAttribute('aria-label', '移除' + noun + ' ' + words[index]);
        remove.setAttribute('title', '移除' + noun + ' ' + words[index]);
        remove.addEventListener('click', function (event) {
          event.preventDefault();
          var at = Number(event.currentTarget.dataset.index);
          words = dropChip(words, at);
          paint();
          input.focus();
          notify();
        });
        chip.appendChild(text);
        chip.appendChild(remove);
        list.appendChild(chip);
      }
    }

    // 回车 / 逗号 / 顿号 / 分号 / 失焦 → 把当前内容收成一个标签。
    function commit(splitSpaces) {
      var incoming = rawText();
      var before = words.length;
      if (!chipWords(incoming, splitSpaces).length) { writeRaw(''); return; }
      words = mergeChips(words, incoming, splitSpaces);
      writeRaw('');
      paint();
      if (words.length !== before) notify();
    }

    // 外部写 input.value = '报名, 截止' 时同步成标签（settings.js 装载卡片就是这么写的）。
    try {
      Object.defineProperty(input, 'value', {
        configurable: true,
        enumerable: true,
        get: function () { return committedText(); },
        set: function (next) {
          writeRaw('');
          words = chipWords(next, false);
          paint();
        }
      });
    } catch (error) { /* 老内核不支持就退回：只保证自己的写入同步，读到的仍是标签串 */ }

    input.addEventListener('keydown', function (event) {
      // 中文输入法选词时的回车不是「提交关键词」，放过它。
      if (event.isComposing || event.keyCode === 229) return;
      var key = event.key;
      if (key === 'Enter' || key === ',' || key === '，' || key === '、' || key === ';' || key === '；') {
        event.preventDefault();
        commit(false);
        return;
      }
      if (key === 'Backspace' && !rawText() && words.length) {
        event.preventDefault();
        words = dropChip(words, words.length - 1);
        paint();
        notify();
      }
    });
    // 输入法整段上屏、或浏览器把逗号直接塞进输入框时，走这里收标签。
    input.addEventListener('input', function (event) {
      if (event.isComposing) return;
      if (CHIP_SEPARATOR.test(rawText())) commit(false);
    });
    input.addEventListener('paste', function (event) {
      var data = event.clipboardData || window.clipboardData;
      if (!data || !data.getData) return;
      var text = data.getData('text/plain') || data.getData('text') || '';
      if (!text) return;
      event.preventDefault();
      words = mergeChips(mergeChips(words, rawText(), false), text, true);
      writeRaw('');
      paint();
      notify();
    });
    input.addEventListener('blur', function () { commit(false); });

    paint();
  }

  function bootKeywordChips() {
    ensureChipStyles(document);
    CHIP_FIELDS.forEach(function (field) {
      var inputs = document.querySelectorAll(field.selector);
      for (var index = 0; index < inputs.length; index += 1) enhanceChips(inputs[index], field.noun);
    });
  }

  // 规则卡片是异步渲染出来的（每次装载/新增都会重建），新出现的输入框也要升级。
  function watchKeywordChips() {
    if (!window.MutationObserver) return;
    var selector = CHIP_FIELDS.map(function (field) { return field.selector; }).join(',');
    function nounOf(node) {
      for (var index = 0; index < CHIP_FIELDS.length; index += 1) {
        if (node.matches && node.matches(CHIP_FIELDS[index].selector)) return CHIP_FIELDS[index].noun;
      }
      return '关键词';
    }
    var observer = new window.MutationObserver(function (records) {
      for (var index = 0; index < records.length; index += 1) {
        var added = records[index].addedNodes || [];
        for (var at = 0; at < added.length; at += 1) {
          var node = added[at];
          if (!node || node.nodeType !== 1) continue;
          if (node.matches && node.matches(selector)) enhanceChips(node, nounOf(node));
          if (node.querySelectorAll) {
            var inner = node.querySelectorAll(selector);
            for (var deep = 0; deep < inner.length; deep += 1) enhanceChips(inner[deep], nounOf(inner[deep]));
          }
        }
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  if (typeof document !== 'undefined' && document.documentElement) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { bootKeywordChips(); watchKeywordChips(); });
    } else {
      bootKeywordChips();
      watchKeywordChips();
    }
  }

  window.NhLabRules = {
    rulesToCards: rulesToCards,
    cardsToRules: cardsToRules,
    chips: { words: chipWords, merge: mergeChips, drop: dropChip, enhance: enhanceChips }
  };
})();
