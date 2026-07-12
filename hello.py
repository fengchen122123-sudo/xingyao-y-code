def bubble_sort(arr):
    """
    冒泡排序算法：
    重复遍历待排序数组，每次比较相邻两个元素，
    如果顺序不对就交换它们，让较大的元素逐渐"冒泡"到数组末尾。
    """
    nums = arr[:]  # 复制原列表，避免修改传入的列表
    n = len(nums)

    # 外层循环：使用 while 循环控制轮次
    i = 0
    swapped = True
    while i < n - 1 and swapped:
        # 提前退出标志：如果本轮没有发生交换，说明已经有序
        swapped = False

        # 内层循环：也使用 while 循环遍历相邻元素
        j = 0
        while j < n - i - 1:
            if nums[j] > nums[j + 1]:
                nums[j], nums[j + 1] = nums[j + 1], nums[j]
                swapped = True
            j += 1

        i += 1

    return nums


if __name__ == "__main__":
    test_array = [64, 34, 25, 12, 22, 11, 90]
    print(f"原始数组: {test_array}")
    sorted_array = bubble_sort(test_array)
    print(f"排序后数组: {sorted_array}")
    print("我是星瑶")
