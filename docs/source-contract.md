# 来源资料约定

养老服务数据湖的受控资料与派生交付副本。

所有人员、机构、地址、业务事件及数值均为合成，不来自真实企业或个人。UTF-8 编码，日期为 ISO 8601，时间附明确时区。有效区间左闭右开，空结束值为尚无结束记录，不据此推断未来。金额使用整数分，非整数数量用十进制字符串；未知不是零。

name、phone、person_id、alias 及可回连的 service_id 都是身份信息。结算例外只保留依据覆盖的受限凭据字段，通用接口和操作日志不能回连它；去身份控制数可按合作方及月份保留。新授权只适用于其时段内新服务，历史清除不复活。请求、例外到期和副本可达性使用可注入时钟或演练状态，不能依赖真实网络。共享文件含其他人员时只清除请求覆盖内容。

## profiles

人员原始记录。

- person_id：人员键
- partner：收集方
- name：姓名
- phone：联系方式
- alias：可回连的内部键

## services

原始服务，不是已清除结果。

- service_id：服务键
- person_id：人员
- partner：合作方
- date：服务日
- fee_fen：金额分

## authorities

依据独立限定范围。

- authority_id：依据
- person_id：人员
- partner：合作方
- fields：字段范围
- from_date：开始
- until_date：结束
- kind：服务或结算

## requests

收到时限制新交付，实际清除须另行完成。

- request_id：请求
- person_id：人员
- partner：空值表示全部
- fields：星号为全部可识别内容
- received_at：收到时间

## managed_export

由原件产生且仍受管理的交付副本。

- service_id：服务
- person_id：人员
- partner：合作方
- date：日期
- fee_fen：分
- phone：联系方式

## copies

真实文件路径与派生边。

- copy_id：副本
- path：相对路径
- parents：上游副本
- availability：演练初始可达性
- identifying_fields：身份字段

## incoming

后续离线补交，payload不是处理答案。

- delivery_id：投递
- received_at：到达时间
- payload：原始行
