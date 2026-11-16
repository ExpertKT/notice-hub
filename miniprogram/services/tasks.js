const GROUPS = [
  ['candidates', '待确认'], ['today', '今天'], ['week', '本周'], ['later', '之后'], ['done', '已完成']
];

function parseBoard(board) {
  const sections = GROUPS.map(([key, label]) => {
    if (!Array.isArray(board[key])) throw new Error('待办分组缺失，请检查服务端接口。');
    return { key, label, items: board[key].map(task => {
      if (!task || typeof task !== 'object' || !Number.isInteger(Number(task.id)) || Number(task.id) <= 0 || typeof task.summary !== 'string') {
        throw new Error('待办内容不完整，请检查服务端接口。');
      }
      return {
        id: task.id, summary: task.summary, deadline: typeof task.deadline === 'string' ? task.deadline.replace('T', ' ') : '未设截止时间',
        groups: Array.isArray(task.groups) ? task.groups.join('、') : '', pinned: Boolean(task.pinned)
      };
    }) };
  });
  return sections.filter(section => section.items.length);
}

module.exports = { parseBoard };
