PLATFORMS = {'github': 'GitHub', 'gitcode': 'GitCode'}

ROLES = {'developer': '开发人员', 'approver': '审批人', 'admin': '管理员'}
ROLE_LEVEL = {'developer': 1, 'approver': 2, 'admin': 3}

# 状态 -> (中文, 徽标颜色)
REPO_STATUS = {
    'active': ('活跃', 'green'),
    'inactive': ('不活跃', 'orange'),
    'archived': ('已归档', 'gray'),
}

APP_STATUS = {
    'pending': ('待审批', 'blue'),
    'approved': ('已通过', 'green'),
    'rejected': ('已驳回', 'red'),
    'withdrawn': ('已撤回', 'gray'),
}

APP_KIND = {'create': '新建仓库', 'archive': '归档仓库'}

VISIBILITY = {'private': '私有', 'public': '公开'}

REPO_SOURCE = {'request': '审批流程', 'import': '扫描导入', 'manual': '手工登记'}

FINDING_ISSUE = {
    'unregistered': ('平台存在，未登记', 'orange'),
    'missing': ('已登记，平台未发现', 'red'),
    'platform_archived': ('平台已归档，登记未同步', 'gray'),
}

RUN_KIND = {'sync': '登记一致性扫描', 'activity': '活跃度检查'}

RUN_STATUS = {
    'running': ('运行中', 'blue'),
    'ok': ('成功', 'green'),
    'partial': ('部分失败', 'orange'),
    'failed': ('失败', 'red'),
}
