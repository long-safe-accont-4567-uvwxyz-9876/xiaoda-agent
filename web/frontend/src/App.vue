onMounted(async () => {
  // 1. 首次运行检测：API Key 未配置 → 跳转 setup 向导
  try {
    const data = await api.getSetupFirstRun()
    if (data?.first_run) {
      router.replace('/setup')
      return
    }
  } catch {
    // 检测失败，继续正常流程
  }
  // 2. 非首次运行：未登录则跳转登录页（已登录的直接进主界面）
  if (!auth.isLoggedIn) {
    router.replace('/login')
  }
  // 3. 已登录：路由守卫会放行，无需额外跳转
})