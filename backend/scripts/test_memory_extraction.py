"""独立测试记忆偏好抽取功能，不依赖完整的 /trip/edit 链路。"""

from app.services.memory_service import extract_preferences_from_instruction


def main():
    test_cases = [
        "我对海鲜过敏，以后规划都别安排海鲜餐厅",
        "这一天节奏更轻松一点，减少固定安排。",
        "我喜欢住经济型酒店，预算有限",
        "这次在大理可以吃辣，平时不太能吃辣",
    ]

    for instruction in test_cases:
        print(f"\n指令：{instruction}")
        result = extract_preferences_from_instruction(instruction)
        if not result:
            print("  抽取结果：空（未识别到值得记住的偏好）")
        else:
            for item in result:
                print(
                    f"  - category={item.category}, key={item.key}, "
                    f"value={item.value}, scope={item.scope}"
                )


if __name__ == "__main__":
    main()