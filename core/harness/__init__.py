"""固定 Harness 内核。

各组件由其同名子模块独立拥有；调用方应从具体子模块导入稳定内部接口，避免
包级聚合导入引入 Journal/MemoryManager 与工具适配器之间的循环依赖。Harness
是静态内核，不参与插件扫描；插件能力仍只通过现有 ToolRegistry 进入工具管线。
"""
