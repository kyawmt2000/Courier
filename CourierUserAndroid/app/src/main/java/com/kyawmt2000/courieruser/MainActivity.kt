package com.kyawmt2000.courieruser

import android.app.Activity
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.kyawmt2000.courieruser.model.ChatMessage
import com.kyawmt2000.courieruser.model.DeliveryOrder
import com.kyawmt2000.courieruser.model.OrderStatus
import com.kyawmt2000.courieruser.model.ParcelType
import com.kyawmt2000.courieruser.model.PaymentMode
import com.kyawmt2000.courieruser.model.PaymentStatus
import com.kyawmt2000.courieruser.model.SettlementStatus
import com.kyawmt2000.courieruser.ui.theme.BlinkBlue
import com.kyawmt2000.courieruser.ui.theme.CourierTheme
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private val viewModel by viewModels<MainViewModel>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            CourierTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    if (viewModel.isLoggedIn) {
                        MainShell(viewModel)
                    } else {
                        LoginScreen(viewModel)
                    }
                }
            }
        }
    }
}

private enum class AccountPage {
    Home,
    PaymentQr,
    Notifications,
    Language
}

private fun platformDeliveryFee(deliveryFee: Double): Double {
    return kotlin.math.round(deliveryFee * if (deliveryFee >= 10000.0) 0.08 else 0.10)
}

private fun riderDeliveryFee(deliveryFee: Double): Double {
    return (deliveryFee - platformDeliveryFee(deliveryFee)).coerceAtLeast(0.0)
}

private data class AddressFields(
    val name: String = "",
    val phone: String = "",
    val building: String = "",
    val street: String = "",
    val city: String = "Yangon",
    val township: String = "Yankin",
    val mapLocation: String = ""
) {
    val isComplete: Boolean
        get() = name.isNotBlank() &&
            phone.isNotBlank() &&
            building.isNotBlank() &&
            street.isNotBlank() &&
            city.isNotBlank() &&
            township.isNotBlank()

    val hasMapLocation: Boolean
        get() = mapLocation.isNotBlank()

    fun asAddress(): String {
        val base = listOf(
            "Name: $name",
            "Phone: $phone",
            "Building: $building",
            "Street: $street",
            "City: $city",
            "Township: $township"
        ).joinToString(", ")
        return if (mapLocation.isBlank()) base else "$base, Google Map Location: $mapLocation"
    }
}

private data class OrderDraft(
    val sender: AddressFields,
    val receiver: AddressFields = AddressFields(name = "Receiver"),
    val goodsAmount: String = "",
    val note: String = "",
    val parcelType: ParcelType = ParcelType.Documents,
    val paymentMode: PaymentMode = PaymentMode.Cod,
    val goodsBytes: ByteArray? = null,
    val proofBytes: ByteArray? = null
)

@Composable
private fun LoginScreen(model: MainViewModel) {
    val context = LocalContext.current
    val googleSignInLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        runCatching {
            LegacyGoogleAccountSignIn.resultFromIntent(result.data)
        }.onSuccess { googleResult ->
            model.loginWithGoogleAccount(googleResult)
        }.onFailure { error ->
            model.showLoginError(error.localizedMessage ?: "Google 登录失败")
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(24.dp),
        verticalArrangement = Arrangement.Center
    ) {
        Text("Blink Express", fontSize = 38.sp, fontWeight = FontWeight.Bold)
        Text(model.t("用户端"), color = Color.Gray)
        Spacer(Modifier.height(28.dp))

        StatusMessages(model)
        Spacer(Modifier.height(18.dp))

        Button(
            onClick = {
                runCatching {
                    LegacyGoogleAccountSignIn.signInIntent(context as Activity)
                }.onSuccess { intent ->
                    googleSignInLauncher.launch(intent)
                }.onFailure { error ->
                    model.showLoginError(error.localizedMessage ?: "Google 登录失败")
                }
            },
            enabled = !model.isLoading,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(if (model.isLoading) model.t("登录中") else "Continue with Gmail")
        }
    }
}

@Composable
private fun MainShell(model: MainViewModel) {
    var selectedTab by remember { mutableStateOf(0) }
    var ordersShowHistory by remember { mutableStateOf(false) }
    var chatConversationId by remember { mutableStateOf<String?>(null) }
    var accountPage by remember { mutableStateOf(AccountPage.Home) }
    var requiredTermsDismissed by remember(model.user?.phone) { mutableStateOf(false) }
    var orderDraft by remember {
        mutableStateOf(
            OrderDraft(
                sender = AddressFields(
                    name = "Sender",
                    phone = model.user?.phone ?: "+959777777777"
                )
            )
        )
    }
    val tabs = listOf(model.t("下单"), model.t("订单"), model.t("聊天"), model.t("我的"))

    Scaffold(
        bottomBar = {
            NavigationBar {
                tabs.forEachIndexed { index, title ->
                    NavigationBarItem(
                        selected = selectedTab == index,
                        onClick = {
                            selectedTab = index
                            if (index == 1) model.loadOrders()
                            if (index == 2) {
                                chatConversationId = null
                                model.loadOrders()
                            }
                            if (index == 3) {
                                accountPage = AccountPage.Home
                                model.loadProfile()
                            }
                        },
                        icon = { Text(tabIcon(index), fontSize = 22.sp) },
                        label = { Text(title) }
                    )
                }
            }
        }
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.background)
                .padding(padding)
        ) {
            when (selectedTab) {
                0 -> HomeScreen(
                    model = model,
                    draft = orderDraft,
                    onDraftChange = { orderDraft = it }
                )
                1 -> OrdersScreen(
                    model = model,
                    showHistory = ordersShowHistory,
                    onShowHistoryChange = { ordersShowHistory = it }
                )
                2 -> ChatScreen(
                    model = model,
                    conversationId = chatConversationId,
                    onOpenConversation = {
                        chatConversationId = it
                        model.loadMessages(it)
                    },
                    onBackToList = {
                        chatConversationId = null
                    }
                )
                else -> AccountScreen(
                    model = model,
                    page = accountPage,
                    onPageChange = { accountPage = it },
                    onOpenOrders = {
                        ordersShowHistory = true
                        model.loadOrders()
                        selectedTab = 1
                    },
                    onOpenCustomerService = {
                        chatConversationId = "main"
                        model.loadMessages("main")
                        selectedTab = 2
                    }
                )
            }
        }
    }
    if (model.user != null && !model.hasAcceptedTerms && !requiredTermsDismissed) {
        TermsDialog(model = model, required = true, onDismiss = { requiredTermsDismissed = true })
    }
}

@Composable
private fun HomeScreen(
    model: MainViewModel,
    draft: OrderDraft,
    onDraftChange: (OrderDraft) -> Unit
) {
    val context = LocalContext.current
    val sender = draft.sender
    val receiver = draft.receiver
    val goodsAmount = draft.goodsAmount
    val note = draft.note
    val parcelType = draft.parcelType
    val paymentMode = draft.paymentMode
    val goodsBytes = draft.goodsBytes
    val proofBytes = draft.proofBytes
    var showPlaceOrderConfirmation by remember { mutableStateOf(false) }
    var placeOrderValidationMessage by remember { mutableStateOf<String?>(null) }

    val goodsPicker = rememberImagePicker { uri ->
        onDraftChange(draft.copy(goodsBytes = readBytes(context, uri)))
    }
    val proofPicker = rememberImagePicker { uri ->
        onDraftChange(draft.copy(proofBytes = readBytes(context, uri)))
    }
    val paymentStatus = model.pendingPayment?.let { PaymentStatus.from(it.status) }
    val goodsValue = goodsAmount.toDoubleOrNull() ?: 0.0
    val pickupAddress = sender.asAddress()
    val dropoffAddress = receiver.asAddress()
    val placeOrder = {
        model.createOrder(
            pickup = pickupAddress,
            dropoff = dropoffAddress,
            parcelType = parcelType,
            paymentMode = paymentMode,
            goodsAmount = goodsValue,
            note = note,
            goodsBytes = goodsBytes,
            onCreated = {
                onDraftChange(OrderDraft(sender = sender))
            }
        )
    }
    val validatePlaceOrder = {
        when {
            !sender.isComplete -> model.t("请填写完整寄件信息")
            !receiver.isComplete -> model.t("请填写完整收货信息")
            model.distanceKm <= 0 -> model.t("请先计算距离和费用")
            goodsValue <= 0 -> model.t("请填写货物价格")
            goodsBytes == null -> model.t("请上传商品图片")
            paymentStatus != PaymentStatus.Confirmed -> model.t("请先提交并等待后台确认付款")
            else -> null
        }
    }

    if (showPlaceOrderConfirmation) {
        AlertDialog(
            onDismissRequest = { showPlaceOrderConfirmation = false },
            title = { Text(model.t("订单已提交")) },
            text = { Text(model.t("后台已收到送货费，要立即下单吗")) },
            dismissButton = {
                OutlinedButton(onClick = { showPlaceOrderConfirmation = false }) {
                    Text(model.t("放弃"))
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        showPlaceOrderConfirmation = false
                        placeOrder()
                    }
                ) {
                    Text(model.t("下单"))
                }
            }
        )
    }
    placeOrderValidationMessage?.let { message ->
        AlertDialog(
            onDismissRequest = { placeOrderValidationMessage = null },
            title = { Text(model.t("无法下单")) },
            text = { Text(message) },
            confirmButton = {
                Button(onClick = { placeOrderValidationMessage = null }) {
                    Text(model.t("确定"))
                }
            }
        )
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item { Text(model.t("下单"), fontSize = 36.sp, fontWeight = FontWeight.Bold) }
        item {
            CardBlock {
                Text(model.t("寄件信息"), fontSize = 20.sp, fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(12.dp))
                AddressForm(
                    model = model,
                    title = "取件地址",
                    value = sender,
                    onChange = { onDraftChange(draft.copy(sender = it)) }
                )
                Spacer(Modifier.height(16.dp))
                AddressForm(
                    model = model,
                    title = "收货地址",
                    value = receiver,
                    onChange = { onDraftChange(draft.copy(receiver = it)) }
                )
            }
        }
        item {
            CardBlock {
                Text(model.t("物品类型"), fontWeight = FontWeight.Bold)
                ChipRow(ParcelType.entries, parcelType, { onDraftChange(draft.copy(parcelType = it)) }) { model.t(it.title) }
                Spacer(Modifier.height(10.dp))
                Text(model.t("付款方式"), fontWeight = FontWeight.Bold)
                ChipRow(PaymentMode.entries, paymentMode, { onDraftChange(draft.copy(paymentMode = it)) }) { model.t(it.title) }
                Text(model.t(paymentMode.detail), color = Color.Gray, fontSize = 13.sp)
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = goodsAmount,
                    onValueChange = { onDraftChange(draft.copy(goodsAmount = it)) },
                    label = { Text(model.t("货物价格 MMK")) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = note,
                    onValueChange = { onDraftChange(draft.copy(note = it)) },
                    label = { Text(model.t("备注，比如门牌号、易碎物品")) },
                    maxLines = 3,
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(Modifier.height(10.dp))
                OutlinedButton(onClick = { goodsPicker.launch("image/*") }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (goodsBytes == null) model.t("选择商品图") else model.t("商品图已选择，可更换"))
                }
                Spacer(Modifier.height(10.dp))
                Button(
                    onClick = { model.estimateDistance(sender.mapLocation, receiver.mapLocation) },
                    enabled = sender.hasMapLocation && receiver.hasMapLocation && !model.isLoading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(model.t("计算距离和费用"))
                }
                if (model.pickupLat != null && model.pickupLng != null && model.dropoffLat != null && model.dropoffLng != null) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "${model.t("取件")} ${model.pickupLat}, ${model.pickupLng}\n${model.t("收货")} ${model.dropoffLat}, ${model.dropoffLng}",
                        color = Color.Gray,
                        fontSize = 12.sp
                    )
                }
            }
        }
        item {
            CardBlock(container = BlinkBlue.copy(alpha = 0.08f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(model.t("预估费用"), color = Color.Gray)
                        Text("${"%.0f".format(model.estimatedPrice)} MMK", fontSize = 30.sp, fontWeight = FontWeight.Bold)
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text(model.t("预计 30-45 分钟"), color = Color.Gray, fontSize = 12.sp)
                        Text("${"%.1f".format(model.distanceKm)} km", color = Color.Gray)
                    }
                }
                Spacer(Modifier.height(12.dp))
                KPayQrCodeBox(model)
                Spacer(Modifier.height(12.dp))
                OutlinedButton(onClick = { proofPicker.launch("image/*") }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (proofBytes == null) model.t("选择 KPay 转账截图") else model.t("KPay 截图已选择，可更换"))
                }
                Spacer(Modifier.height(8.dp))
                Button(
                    onClick = { proofBytes?.let { model.submitDeliveryFeePayment(paymentMode, goodsValue, it) } },
                    enabled = proofBytes != null && model.distanceKm > 0 && goodsValue > 0 &&
                        paymentStatus != PaymentStatus.Confirmed && !model.isLoading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(model.t("提交付款"))
                }
                model.pendingPayment?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(model.t("付款状态：%s").format(model.t(it.statusTitle)), color = BlinkBlue)
                    OutlinedButton(onClick = { model.refreshPayment() }, modifier = Modifier.fillMaxWidth()) {
                        Text(model.t("刷新付款状态"))
                    }
                }
                Spacer(Modifier.height(12.dp))
                Button(
                    onClick = {
                        val validationMessage = validatePlaceOrder()
                        if (validationMessage == null) {
                            showPlaceOrderConfirmation = true
                        } else {
                            placeOrderValidationMessage = validationMessage
                        }
                    },
                    enabled = paymentStatus == PaymentStatus.Confirmed && !model.isLoading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(if (model.isLoading) model.t("提交中") else model.t("立即下单"))
                }
                StatusMessages(model)
            }
        }
    }
}

@Composable
private fun OrdersScreen(
    model: MainViewModel,
    showHistory: Boolean,
    onShowHistoryChange: (Boolean) -> Unit
) {
    val list = if (showHistory) model.historyOrders else model.activeOrders
    var selectedOrder by remember { mutableStateOf<DeliveryOrder?>(null) }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(model.t("订单"), fontSize = 36.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                Button(onClick = { model.loadOrders() }, enabled = !model.isLoading) { Text(model.t("刷新")) }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(selected = !showHistory, onClick = { onShowHistoryChange(false) }, label = { Text(model.t("进行中")) })
                FilterChip(selected = showHistory, onClick = { onShowHistoryChange(true) }, label = { Text(model.t("我的订单")) })
            }
        }
        if (list.isEmpty()) {
            item { EmptyCard(if (showHistory) model.t("暂无历史订单") else model.t("暂无进行中订单")) }
        } else {
            items(list, key = { it.id }) { order ->
                OrderRow(order, model, onOpenDetail = { selectedOrder = order })
            }
        }
    }
    selectedOrder?.let { order ->
        OrderDetailDialog(order = order, model = model, onDismiss = { selectedOrder = null })
    }
}

@Composable
private fun OrderRow(order: DeliveryOrder, model: MainViewModel, onOpenDetail: () -> Unit) {
    var settlementName by remember { mutableStateOf(model.user?.nickname ?: "") }
    val context = LocalContext.current
    var qrBytes by remember { mutableStateOf<ByteArray?>(null) }
    val qrPicker = rememberImagePicker { uri -> qrBytes = readBytes(context, uri) }
    val canRequestSettlement = order.statusType == OrderStatus.Completed &&
        order.paymentMode == PaymentMode.Cod.apiValue &&
        SettlementStatus.from(order.settlementStatus) == SettlementStatus.Pending

    CardBlock(modifier = Modifier.clickable(onClick = onOpenDetail)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("#${order.shortCode}", fontSize = 20.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
            Text(model.t(order.statusTitle), color = BlinkBlue)
        }
        Spacer(Modifier.height(8.dp))
        Text(order.pickupAddress, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Text(order.dropoffAddress, color = Color.Gray, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(8.dp))
        Text(
            "${model.t(order.paymentTitle)}  ${model.t("付款")}：${model.t(order.userPaymentTitle)}  ${model.t("结算")}：${model.t(order.settlementTitle)}",
            color = Color.Gray,
            fontSize = 13.sp
        )
        Row {
            Text(order.parcelType, color = Color.Gray)
            Spacer(Modifier.width(12.dp))
            Text("${"%.1f".format(order.distanceKm)} km", color = Color.Gray)
            Spacer(Modifier.weight(1f))
            Text("${"%.0f".format(order.displayRiderDeliveryFee)} MMK", fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.height(8.dp))
        FeeBreakdown(
            gross = order.grossDeliveryFee,
            platform = order.displayPlatformDeliveryFee,
            rider = order.displayRiderDeliveryFee,
            translate = model::t
        )
        order.riderName?.let {
            Spacer(Modifier.height(6.dp))
            Text("${model.t("骑手")}：$it", color = BlinkBlue)
        }
        if (order.goodsImageUrl != null) {
            Text("${model.t("商品图")}：${order.goodsImageUrl}", color = Color.Gray, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        if (order.userSettlementBillCreatedAt != null) {
            Spacer(Modifier.height(10.dp))
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(BlinkBlue.copy(alpha = 0.08f), RoundedCornerShape(10.dp))
                    .padding(12.dp)
            ) {
                Text(
                    order.userSettlementBillMessage
                        ?: "${model.t("货费")} ${"%.0f".format(order.userSettlementBillAmount ?: order.goodsAmount)} MMK ${model.t("已转给用户，请查收")}",
                    color = BlinkBlue,
                    fontWeight = FontWeight.Bold
                )
            }
        }
        if (order.statusType == OrderStatus.Matching || order.statusType == OrderStatus.Accepted) {
            Spacer(Modifier.height(10.dp))
            OutlinedButton(onClick = { model.cancelOrder(order) }, modifier = Modifier.fillMaxWidth()) {
                Text(model.t("取消订单"))
            }
        }
        if (canRequestSettlement) {
            Spacer(Modifier.height(10.dp))
            OutlinedTextField(settlementName, { settlementName = it }, label = { Text(model.t("收款名字")) }, modifier = Modifier.fillMaxWidth())
            OutlinedButton(onClick = { qrPicker.launch("image/*") }, modifier = Modifier.fillMaxWidth()) {
                Text(if (qrBytes == null && model.user?.paymentQrUrl == null) model.t("选择收款二维码") else model.t("收款二维码已准备"))
            }
            Button(
                onClick = { model.requestSettlement(order, settlementName, qrBytes) },
                enabled = settlementName.isNotBlank() && !model.isLoading,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(model.t("提醒平台转账货费"))
            }
        }
    }
}

@Composable
private fun OrderDetailDialog(order: DeliveryOrder, model: MainViewModel, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            Button(onClick = onDismiss) {
                Text(model.t("返回"))
            }
        },
        title = { Text("${model.t("订单")} #${order.shortCode}") },
        text = {
            Column(
                modifier = Modifier
                    .heightIn(max = 520.dp)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                DetailLine(model.t("取件地址"), order.pickupAddress)
                DetailLine(model.t("收货地址"), order.dropoffAddress)
                DetailLine(model.t("付款方式"), model.t(order.paymentTitle))
                DetailLine(model.t("货物价格 MMK"), "${"%.0f".format(order.goodsAmount)} MMK")
                DetailLine(model.t("预估费用"), "${"%.0f".format(order.grossDeliveryFee)} MMK")
                DetailLine(model.t("物品类型"), model.t(order.parcelType))
                DetailLine(model.t("付款状态"), model.t(order.userPaymentTitle))
                if (order.note.isNotBlank()) {
                    DetailLine(model.t("备注，比如门牌号、易碎物品"), order.note)
                }
                order.goodsImageUrl?.let { url ->
                    Text(model.t("商品图"), fontWeight = FontWeight.Bold)
                    AsyncImage(
                        model = url,
                        contentDescription = model.t("商品图"),
                        contentScale = ContentScale.Crop,
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(180.dp)
                            .background(Color.LightGray, RoundedCornerShape(8.dp))
                    )
                }
            }
        }
    )
}

@Composable
private fun DetailLine(label: String, value: String) {
    Column {
        Text(label, color = Color.Gray, fontSize = 12.sp)
        Text(value)
    }
}

@Composable
private fun KPayQrCodeBox(model: MainViewModel) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        modifier = Modifier
            .fillMaxWidth()
            .background(Color.White.copy(alpha = 0.65f), RoundedCornerShape(8.dp))
            .padding(12.dp)
    ) {
        Text(model.t("KPay 付款二维码"), fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(8.dp))
        Image(
            painter = painterResource(id = R.drawable.kpay_qr_code),
            contentDescription = model.t("KPay 付款二维码"),
            contentScale = ContentScale.Fit,
            modifier = Modifier.size(220.dp)
        )
    }
}

@Composable
private fun FeeBreakdown(
    gross: Double,
    platform: Double,
    rider: Double,
    translate: (String) -> String
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(Color.White.copy(alpha = 0.65f), RoundedCornerShape(8.dp))
            .padding(10.dp)
    ) {
        FeeLine(translate("原送货费"), gross)
        FeeLine(translate("平台扣费"), platform)
        FeeLine(translate("最终送货费"), rider, bold = true)
    }
}

@Composable
private fun FeeLine(label: String, amount: Double, bold: Boolean = false) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = Color.Gray, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Text(
            "${"%.0f".format(amount)} MMK",
            fontSize = 13.sp,
            fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal
        )
    }
}

@Composable
private fun ChatScreen(
    model: MainViewModel,
    conversationId: String?,
    onOpenConversation: (String) -> Unit,
    onBackToList: () -> Unit
) {
    if (conversationId == null) {
        ChatConversationList(model, onOpenConversation)
    } else {
        ChatThreadScreen(model, conversationId, onBackToList)
    }
}

@Composable
private fun ChatConversationList(
    model: MainViewModel,
    onOpenConversation: (String) -> Unit
) {
    val orderConversations = (model.activeOrders + model.historyOrders).take(20)

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Text(model.t("聊天"), fontSize = 36.sp, fontWeight = FontWeight.Bold)
            Text(model.t("点开客服或订单号后进入聊天"), color = Color.Gray, fontSize = 13.sp)
            StatusMessages(model)
        }
        item {
            ChatConversationRow(
                title = model.t("客服聊天"),
                subtitle = model.t("客服中心"),
                onClick = { onOpenConversation("main") }
            )
        }
        if (orderConversations.isEmpty()) {
            item { EmptyCard(model.t("暂无订单聊天")) }
        } else {
            item {
                Text(model.t("订单聊天"), color = Color.Gray, fontSize = 13.sp)
            }
            items(orderConversations, key = { it.id }) { order ->
                ChatConversationRow(
                    title = "#${order.shortCode}",
                    subtitle = "${model.t(order.statusTitle)}  ${order.pickupAddress}",
                    onClick = { onOpenConversation("order:${order.id.lowercase()}") }
                )
            }
        }
    }
}

@Composable
private fun ChatConversationRow(
    title: String,
    subtitle: String,
    onClick: () -> Unit
) {
    CardBlock {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(title, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                Text(subtitle, color = Color.Gray, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Button(onClick = onClick) {
                Text(">")
            }
        }
    }
}

@Composable
private fun ChatThreadScreen(
    model: MainViewModel,
    conversationId: String,
    onBackToList: () -> Unit
) {
    val context = LocalContext.current
    var draft by remember { mutableStateOf("") }
    var imageBytes by remember { mutableStateOf<ByteArray?>(null) }
    val imagePicker = rememberImagePicker { uri -> imageBytes = readBytes(context, uri) }
    val order = (model.activeOrders + model.historyOrders)
        .firstOrNull { conversationId == "order:${it.id.lowercase()}" }
    val title = if (conversationId == "main") model.t("客服聊天") else order?.let { "#${it.shortCode}" } ?: model.t("订单聊天")

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(onClick = onBackToList) {
                    Text(model.t("返回"))
                }
                Spacer(Modifier.width(10.dp))
                Text(title, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            }
            StatusMessages(model)
        }
        if (model.chatMessages.isEmpty()) {
            item { EmptyCard(model.t("暂无聊天消息")) }
        } else {
            items(model.chatMessages, key = { it.id }) { message ->
                ChatBubble(message, model)
            }
        }
        item {
            CardBlock {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    label = { Text(model.t("输入消息")) },
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = { imagePicker.launch("image/*") }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (imageBytes == null) model.t("选择图片") else model.t("图片已选择，可更换"))
                }
                Spacer(Modifier.height(8.dp))
                Button(
                    onClick = {
                        model.sendMessage(
                            conversationId = conversationId,
                            text = draft,
                            imageBytes = imageBytes
                        )
                        draft = ""
                        imageBytes = null
                    },
                    enabled = (draft.isNotBlank() || imageBytes != null) && !model.isLoading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(model.t("发送"))
                }
            }
        }
    }
}

@Composable
private fun ChatBubble(message: ChatMessage, model: MainViewModel) {
    val isMine = message.senderType == "user"
    CardBlock(container = if (isMine) BlinkBlue.copy(alpha = 0.10f) else Color.White) {
        Text(message.senderName, fontWeight = FontWeight.Bold)
        if (message.text.isNotBlank()) {
            Text(message.text)
        }
        message.imageUrl?.let {
            Text("${model.t("图片")}：$it", color = Color.Gray, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        message.createdAt?.let {
            Text(it, color = Color.Gray, fontSize = 12.sp)
        }
    }
}

@Composable
private fun AccountScreen(
    model: MainViewModel,
    page: AccountPage,
    onPageChange: (AccountPage) -> Unit,
    onOpenOrders: () -> Unit,
    onOpenCustomerService: () -> Unit
) {
    when (page) {
        AccountPage.Home -> AccountHomeScreen(
            model = model,
            onOpenOrders = onOpenOrders,
            onOpenCustomerService = onOpenCustomerService,
            onOpenPaymentQr = { onPageChange(AccountPage.PaymentQr) },
            onOpenNotifications = { onPageChange(AccountPage.Notifications) },
            onOpenLanguage = { onPageChange(AccountPage.Language) }
        )
        AccountPage.PaymentQr -> PaymentQrScreen(model) { onPageChange(AccountPage.Home) }
        AccountPage.Notifications -> NotificationsScreen(model) { onPageChange(AccountPage.Home) }
        AccountPage.Language -> LanguageScreen(model) { onPageChange(AccountPage.Home) }
    }
}

@Composable
private fun AccountHomeScreen(
    model: MainViewModel,
    onOpenOrders: () -> Unit,
    onOpenCustomerService: () -> Unit,
    onOpenPaymentQr: () -> Unit,
    onOpenNotifications: () -> Unit,
    onOpenLanguage: () -> Unit
) {
    val context = LocalContext.current
    var nickname by remember(model.user?.nickname) { mutableStateOf(model.user?.nickname ?: "用户") }
    var avatarUri by remember { mutableStateOf<Uri?>(null) }
    var avatarBytes by remember { mutableStateOf<ByteArray?>(null) }
    var showTerms by remember { mutableStateOf(false) }
    var showDeleteAccountConfirmation by remember { mutableStateOf(false) }
    val avatarPicker = rememberImagePicker { uri ->
        avatarUri = uri
        avatarBytes = readBytes(context, uri)
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item { Text(model.t("我的"), fontSize = 36.sp, fontWeight = FontWeight.Bold) }
        item {
            CardBlock {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    val avatarModel = avatarUri ?: model.user?.avatarUrl
                    if (avatarModel != null) {
                        AsyncImage(
                            model = avatarModel,
                            contentDescription = model.t("头像已选择"),
                            contentScale = ContentScale.Crop,
                            modifier = Modifier
                                .size(72.dp)
                                .clip(RoundedCornerShape(36.dp))
                                .background(Color.LightGray)
                        )
                    } else {
                        Box(
                            modifier = Modifier
                                .size(72.dp)
                                .clip(RoundedCornerShape(36.dp))
                                .background(BlinkBlue.copy(alpha = 0.12f)),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                nickname.trim().take(1).ifBlank { "U" }.uppercase(),
                                color = BlinkBlue,
                                fontSize = 28.sp,
                                fontWeight = FontWeight.Bold
                            )
                        }
                    }
                    Spacer(Modifier.width(12.dp))
                    Column {
                        Text(nickname, fontWeight = FontWeight.Bold)
                        Text(model.user?.phone.orEmpty(), color = Color.Gray)
                    }
                }
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(nickname, { nickname = it }, label = { Text(model.t("用户名")) }, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = { avatarPicker.launch("image/*") }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (avatarBytes == null) model.t("更换头像") else model.t("头像已选择"))
                }
                Button(
                    onClick = { model.updateProfile(nickname, avatarBytes, null) },
                    enabled = nickname.isNotBlank() && !model.isLoading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(model.t("保存资料"))
                }
                StatusMessages(model)
            }
        }
        item {
            CardBlock {
                Button(onClick = onOpenOrders, modifier = Modifier.fillMaxWidth()) {
                    Text(model.t("我的订单"))
                }
                Spacer(Modifier.height(8.dp))
                Button(onClick = onOpenPaymentQr, modifier = Modifier.fillMaxWidth()) {
                    Text(if (model.user?.paymentQrUrl == null) model.t("我的收款码（未上传）") else model.t("我的收款码"))
                }
                model.user?.paymentQrUrl?.let { paymentQrUrl ->
                    Spacer(Modifier.height(10.dp))
                    AsyncImage(
                        model = paymentQrUrl,
                        contentDescription = model.t("我的收款码"),
                        contentScale = ContentScale.Fit,
                        modifier = Modifier
                            .size(140.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .background(Color.White)
                            .padding(8.dp)
                            .align(Alignment.CenterHorizontally)
                    )
                }
                Spacer(Modifier.height(8.dp))
                Button(onClick = onOpenCustomerService, modifier = Modifier.fillMaxWidth()) {
                    Text(model.t("客服中心"))
                }
                Spacer(Modifier.height(8.dp))
                Button(onClick = onOpenNotifications, modifier = Modifier.fillMaxWidth()) {
                    Text("${model.t("通知")} (${model.notifications.size})")
                }
                Spacer(Modifier.height(8.dp))
                Button(onClick = onOpenLanguage, modifier = Modifier.fillMaxWidth()) {
                    Text("${model.t("语言设置")}：${model.language.title}")
                }
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = { showTerms = true }, modifier = Modifier.fillMaxWidth()) {
                    Text(model.t("服务条款"))
                }
                Text(
                    if (model.hasAcceptedTerms) model.t("已同意服务条款") else model.t("请阅读并同意服务条款"),
                    color = if (model.hasAcceptedTerms) BlinkBlue else Color(0xFFDC2626),
                    fontSize = 13.sp
                )
            }
        }
        item {
            CardBlock {
                Button(onClick = { model.logout() }, modifier = Modifier.fillMaxWidth()) {
                    Text(model.t("退出登录"))
                }
                Spacer(Modifier.height(8.dp))
                OutlinedButton(
                    onClick = { showDeleteAccountConfirmation = true },
                    enabled = !model.isLoading,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(model.t("删除账号"), color = Color(0xFFDC2626))
                }
            }
        }
    }
    if (showTerms) {
        TermsDialog(model = model, required = false, onDismiss = { showTerms = false })
    }
    if (showDeleteAccountConfirmation) {
        AlertDialog(
            onDismissRequest = { showDeleteAccountConfirmation = false },
            title = { Text(model.t("删除账号")) },
            text = { Text(model.t("删除账号后，你的账号资料会被删除，并会退出登录。")) },
            confirmButton = {
                Button(
                    onClick = {
                        showDeleteAccountConfirmation = false
                        model.deleteAccount()
                    },
                    enabled = !model.isLoading
                ) {
                    Text(model.t("确认删除"))
                }
            },
            dismissButton = {
                OutlinedButton(onClick = { showDeleteAccountConfirmation = false }) {
                    Text(model.t("取消"))
                }
            }
        )
    }
}

@Composable
private fun TermsDialog(model: MainViewModel, required: Boolean, onDismiss: () -> Unit) {
    var checked by remember(model.hasAcceptedTerms) { mutableStateOf(model.hasAcceptedTerms) }
    AlertDialog(
        onDismissRequest = { if (!required) onDismiss() },
        confirmButton = {
            Button(
                onClick = {
                    model.acceptTerms(onAccepted = onDismiss)
                },
                enabled = checked && !model.isLoading
            ) {
                Text(model.t("同意"))
            }
        },
        dismissButton = {
            if (!required) {
                OutlinedButton(onClick = onDismiss) {
                    Text(model.t("返回"))
                }
            }
        },
        title = { Text(model.t("服务条款")) },
        text = {
            Column {
                Text(
                    termsBody(model.language),
                    modifier = Modifier
                        .heightIn(max = 360.dp)
                        .verticalScroll(rememberScrollState())
                )
                Spacer(Modifier.height(12.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = checked, onCheckedChange = { checked = it })
                    Text(model.t("我已阅读并同意服务条款"))
                }
                if (!model.hasAcceptedTerms) {
                    StatusMessages(model)
                }
            }
        }
    )
}

@Composable
private fun PaymentQrScreen(model: MainViewModel, onBack: () -> Unit) {
    val context = LocalContext.current
    var nickname by remember(model.user?.nickname) { mutableStateOf(model.user?.nickname ?: "用户") }
    var paymentQrBytes by remember { mutableStateOf<ByteArray?>(null) }
    val qrPicker = rememberImagePicker { uri -> paymentQrBytes = readBytes(context, uri) }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(model.t("我的收款码"), fontSize = 32.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                OutlinedButton(onClick = onBack) { Text(model.t("返回")) }
            }
        }
        item {
            CardBlock(container = BlinkBlue.copy(alpha = 0.08f)) {
                Text(model.t("平台结算转账时会用这个二维码付款。"), color = Color.Gray)
                Spacer(Modifier.height(12.dp))
                Text(if (model.user?.paymentQrUrl == null) model.t("当前未上传") else model.t("当前收款码已上传"), fontWeight = FontWeight.Bold)
                model.user?.paymentQrUrl?.let { paymentQrUrl ->
                    Spacer(Modifier.height(10.dp))
                    AsyncImage(
                        model = paymentQrUrl,
                        contentDescription = model.t("我的收款码"),
                        contentScale = ContentScale.Fit,
                        modifier = Modifier
                            .size(220.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .background(Color.White)
                            .padding(8.dp)
                    )
                }
                Spacer(Modifier.height(12.dp))
                OutlinedTextField(nickname, { nickname = it }, label = { Text(model.t("收款名字")) }, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedButton(onClick = { qrPicker.launch("image/*") }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (paymentQrBytes == null) model.t("选择收款码") else model.t("收款码已选择，可更换"))
                }
                Spacer(Modifier.height(8.dp))
                Button(
                    onClick = { model.updateProfile(nickname, null, paymentQrBytes) },
                    enabled = !model.isLoading && nickname.isNotBlank() && paymentQrBytes != null,
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(model.t("保存收款码"))
                }
                StatusMessages(model)
            }
        }
    }
}

@Composable
private fun NotificationsScreen(model: MainViewModel, onBack: () -> Unit) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(model.t("通知"), fontSize = 32.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                OutlinedButton(onClick = onBack) { Text(model.t("返回")) }
            }
            Button(onClick = { model.clearNotifications() }, enabled = model.notifications.isNotEmpty()) {
                Text(model.t("清空"))
            }
        }
        if (model.notifications.isEmpty()) {
            item { EmptyCard(model.t("暂无通知")) }
        } else {
            items(model.notifications, key = { it.id }) { notification ->
                CardBlock {
                    Text(notification.title, fontWeight = FontWeight.Bold)
                    Text(notification.message, color = Color.Gray)
                    Text(notification.createdAt, color = Color.Gray, fontSize = 12.sp)
                }
            }
        }
    }
}

@Composable
private fun LanguageScreen(model: MainViewModel, onBack: () -> Unit) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(model.t("语言设置"), fontSize = 32.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                OutlinedButton(onClick = onBack) { Text(model.t("返回")) }
            }
        }
        items(AppLanguage.entries, key = { it.name }) { language ->
            CardBlock(container = if (model.language == language) BlinkBlue.copy(alpha = 0.10f) else Color.White) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(language.title, fontSize = 18.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                    Button(onClick = { model.chooseLanguage(language) }) {
                        Text(if (model.language == language) model.t("已选择") else model.t("选择"))
                    }
                }
            }
        }
    }
}

@Composable
private fun AddressForm(
    model: MainViewModel,
    title: String,
    value: AddressFields,
    onChange: (AddressFields) -> Unit
) {
    val cities = listOf("Yangon", "Mandalay", "Naypyidaw", "Bago", "Mawlamyine", "Taunggyi", "Taungoo")
    val townships = when (value.city) {
        "Yangon" -> listOf(
            "Ahlone",
            "Bahan",
            "Dagon",
            "Kamayut",
            "Kyauktada",
            "Kyeemyindaing",
            "Lanmadaw",
            "Latha",
            "Pabedan",
            "Sanchaung",
            "Botahtaung",
            "Dagon Myothit (East)",
            "Dagon Myothit (North)",
            "Dagon Myothit (Seikkan)",
            "Dagon Myothit (South)",
            "Dawbon",
            "Mingalartaungnyunt",
            "North Okkalapa",
            "Pazundaung",
            "South Okkalapa",
            "Tamwe",
            "Thaketa",
            "Thingangyun",
            "Yankin",
            "Hlaing",
            "Hlaingthaya",
            "Hmawbi",
            "Htantabin",
            "Insein",
            "Mayangone",
            "Mingaladon",
            "Shwepyitha",
            "Cocokyun",
            "Dala",
            "Kawhmu",
            "Kayan",
            "Kungyangon",
            "Kyauktan",
            "Seikkan",
            "Seikkyi Kanaungto",
            "Thanlyin",
            "Thongwa",
            "Twantay"
        )
        "Mandalay" -> listOf("Aungmyethazan", "Chanayethazan", "Mahaaungmye", "Chanmyathazi", "Pyigyidagun", "Patheingyi")
        "Naypyidaw" -> listOf(
            "Zabuthiri",
            "Dekkhinathiri",
            "Pobbathiri",
            "Ottarathiri",
            "Zeyathiri",
            "Pyinmana",
            "Lewe",
            "Tatkone"
        )
        "Bago" -> listOf("Bago")
        "Mawlamyine" -> listOf("Mawlamyine")
        "Taunggyi" -> listOf("Taunggyi")
        "Taungoo" -> listOf("Taungoo")
        else -> emptyList()
    }

    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(model.t(title), fontWeight = FontWeight.Bold, color = BlinkBlue)
        OutlinedTextField(
            value = value.name,
            onValueChange = { onChange(value.copy(name = it)) },
            label = { Text(model.t("Name")) },
            modifier = Modifier.fillMaxWidth()
        )
        OutlinedTextField(
            value = value.phone,
            onValueChange = { onChange(value.copy(phone = it)) },
            label = { Text(model.t("Phone No.")) },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
            modifier = Modifier.fillMaxWidth()
        )
        OutlinedTextField(
            value = value.building,
            onValueChange = { onChange(value.copy(building = it)) },
            label = { Text(model.t("Building")) },
            modifier = Modifier.fillMaxWidth()
        )
        OutlinedTextField(
            value = value.street,
            onValueChange = { onChange(value.copy(street = it)) },
            label = { Text(model.t("Street")) },
            modifier = Modifier.fillMaxWidth()
        )
        DropdownField(
            model = model,
            label = "City",
            value = value.city,
            options = cities,
            onSelected = { city ->
                val nextTownship = when (city) {
                    "Yangon" -> "Yankin"
                    "Mandalay" -> "Aungmyethazan"
                    "Naypyidaw" -> "Zabuthiri"
                    "Bago" -> "Bago"
                    "Mawlamyine" -> "Mawlamyine"
                    "Taunggyi" -> "Taunggyi"
                    "Taungoo" -> "Taungoo"
                    else -> ""
                }
                onChange(value.copy(city = city, township = nextTownship))
            }
        )
        DropdownField(
            model = model,
            label = "Township",
            value = value.township,
            options = townships,
            onSelected = { onChange(value.copy(township = it)) }
        )
        OutlinedTextField(
            value = value.mapLocation,
            onValueChange = { onChange(value.copy(mapLocation = it)) },
            label = { Text(model.t("Google Map Location (optional)")) },
            placeholder = { Text(model.t("Paste a map link if available")) },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.fillMaxWidth()
        )
    }
}

@Composable
private fun DropdownField(
    model: MainViewModel,
    label: String,
    value: String,
    options: List<String>,
    onSelected: (String) -> Unit
) {
    var expanded by remember { mutableStateOf(false) }
    Column {
        Text(model.t(label), color = Color.Gray, fontSize = 13.sp)
        Box {
            OutlinedButton(
                onClick = { expanded = true },
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(value, modifier = Modifier.weight(1f), color = BlinkBlue)
                Text("⌄", color = BlinkBlue)
            }
            DropdownMenu(
                expanded = expanded,
                onDismissRequest = { expanded = false },
                modifier = Modifier
                    .fillMaxWidth(0.82f)
                    .heightIn(max = 240.dp)
            ) {
                options.forEach { option ->
                    DropdownMenuItem(
                        text = { Text(option) },
                        onClick = {
                            expanded = false
                            onSelected(option)
                        }
                    )
                }
            }
        }
    }
}

@Composable
private fun StatusMessages(model: MainViewModel) {
    model.errorMessage?.let {
        Spacer(Modifier.height(8.dp))
        Text(it, color = Color(0xFFDC2626))
    }
    model.helperMessage?.let {
        Spacer(Modifier.height(8.dp))
        Text(it, color = BlinkBlue)
    }
}

@Composable
private fun CardBlock(
    container: Color = Color.White,
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = container),
        shape = RoundedCornerShape(8.dp),
        modifier = modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(16.dp), content = content)
    }
}

@Composable
private fun EmptyCard(text: String) {
    CardBlock {
        Text(text, color = Color.Gray)
    }
}

@Composable
private fun <T> ChipRow(
    values: List<T>,
    selected: T,
    onSelected: (T) -> Unit,
    label: (T) -> String
) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        values.forEach { value ->
            FilterChip(
                selected = selected == value,
                onClick = { onSelected(value) },
                label = { Text(label(value)) }
            )
        }
    }
}

@Composable
private fun rememberImagePicker(onPicked: (Uri?) -> Unit) =
    rememberLauncherForActivityResult(ActivityResultContracts.GetContent(), onPicked)

private fun readBytes(context: android.content.Context, uri: Uri?): ByteArray? {
    return uri?.let {
        context.contentResolver.openInputStream(it)?.use { stream -> stream.readBytes() }
    }
}

private fun termsBody(language: AppLanguage): String {
    return when (language) {
        AppLanguage.English -> """
            1. Blink is a delivery matching platform. We provide order, rider matching, chat, payment confirmation, and settlement services.

            2. Users may only send lawful items. Weapons, drugs, controlled goods, illegal goods, or items prohibited by government rules are not allowed. Responsibility for illegal items belongs to the sender, receiver, and related rider.

            3. Pickup and drop-off mainly follow the Google Map Location. Users must confirm the location is correct. Wrong locations or unclear addresses may cause delays, extra fees, failed delivery, or loss, and the user is responsible.

            4. After an order is completed and confirmed, Blink settles delivery fees, item payments, and rider deposits according to the platform rules.

            5. Users must provide true order details, payment screenshots, and payment QR codes. Riders must deliver lawfully and safely, and provide correct settlement information.

            6. If a rider damages an item during pickup, transport, or delivery, the rider must compensate according to the actual loss and platform rules.

            7. To reduce damage risk, users must pack parcels properly. If a parcel is fragile, poorly packed, leaking, unsafe, or likely to be damaged, the rider has the right to cancel the delivery before pickup or when the risk is discovered.

            8. Blink may update these terms. Continuing to use Blink means accepting the updated terms.

            Support WhatsApp: +95 942 459 4930.
        """.trimIndent()
        AppLanguage.Burmese -> """
            ၁။ Blink သည် ပို့ဆောင်ရေးချိတ်ဆက်ပေးသော platform ဖြစ်ပြီး order, rider ချိတ်ဆက်မှု, chat, ငွေပေးချေမှုအတည်ပြုခြင်းနှင့် ငွေရှင်းခြင်း ဝန်ဆောင်မှုများပေးပါသည်။

            ၂။ တရားဝင်ပစ္စည်းများသာ ပို့နိုင်ပါသည်။ လက်နက်၊ မူးယစ်ဆေး၊ ထိန်းချုပ်ပစ္စည်း၊ တရားမဝင်ပစ္စည်း သို့မဟုတ် အစိုးရမှတားမြစ်ထားသောပစ္စည်းများ မပို့ရပါ။ တရားမဝင်ပစ္စည်းဖြစ်ပါက ပို့သူ၊ လက်ခံသူနှင့် သက်ဆိုင်ရာ rider တို့တွင် တာဝန်ရှိပါသည်။

            ၃။ ပစ္စည်းယူရန်နှင့် ပို့ရန်နေရာများသည် Google Map Location ကို အဓိကထားပါသည်။ အသုံးပြုသူသည် location မှန်ကန်ကြောင်း စစ်ဆေးရပါမည်။ Location မှားခြင်း သို့မဟုတ် လိပ်စာမရှင်းခြင်းကြောင့် နောက်ကျမှု၊ အပိုကုန်ကျစရိတ် သို့မဟုတ် ဆုံးရှုံးမှု ဖြစ်ပါက အသုံးပြုသူတွင် တာဝန်ရှိပါသည်။

            ၄။ Order ပြီးဆုံးပြီး အတည်ပြုပြီးနောက် Blink သည် platform စည်းမျဉ်းအတိုင်း ပို့ခ၊ ပစ္စည်းဖိုးနှင့် rider deposit များကို ငွေရှင်းပေးပါမည်။

            ၅။ အသုံးပြုသူသည် order အချက်အလက်၊ payment screenshot နှင့် QR code တို့ကို မှန်ကန်စွာ ပေးရပါမည်။ Rider သည် တရားဝင်၊ လုံခြုံစွာ ပို့ဆောင်ပြီး settlement အချက်အလက်မှန်ကန်စွာ ပေးရပါမည်။

            ၆။ Rider သည် ပစ္စည်းယူခြင်း၊ ပို့ဆောင်ခြင်း သို့မဟုတ် ပို့ပြီးချိန်တွင် ပါဆယ် သို့မဟုတ် ပစ္စည်းကို ပျက်စီးစေပါက အမှန်တကယ်ဆုံးရှုံးမှုနှင့် platform စည်းမျဉ်းအတိုင်း လျော်ကြေးပေးရပါမည်။

            ၇။ ပျက်စီးမှုမဖြစ်စေရန် အသုံးပြုသူသည် ပါဆယ်ကို သေချာစွာထုပ်ပိုးရပါမည်။ ပါဆယ်သည် လွယ်ကူစွာပျက်စီးနိုင်ခြင်း၊ ထုပ်ပိုးမှုမပြည့်စုံခြင်း၊ အရည်ယိုခြင်း၊ မလုံခြုံခြင်း သို့မဟုတ် ပျက်စီးနိုင်ခြေများခြင်းရှိပါက Rider သည် ပစ္စည်းမယူမီ သို့မဟုတ် အန္တရာယ်ကို တွေ့ရှိချိန်တွင် delivery ကို ပယ်ဖျက်ခွင့်ရှိပါသည်။

            ၈။ Blink သည် ဤစည်းမျဉ်းများကို ပြင်ဆင်နိုင်ပါသည်။ ဆက်လက်အသုံးပြုခြင်းသည် ပြင်ဆင်ထားသောစည်းမျဉ်းများကို လက်ခံခြင်းဖြစ်ပါသည်။

            Support WhatsApp: +95 942 459 4930.
        """.trimIndent()
        AppLanguage.Chinese -> """
            1. Blink 是配送撮合平台，提供下单、接单、聊天、付款确认和结算服务。

            2. 用户只能寄送合法物品。禁止寄送武器、毒品、管制品、违法物品或政府禁止配送的物品。若出现违法物品，责任由相关寄件人、收货人和骑手承担。

            3. 取件和送货地址主要以 Google Map Location 为准。用户必须确认定位正确。因地址或定位错误造成延误、额外费用或损失，由用户承担。

            4. 订单完成并确认后，平台会按规则结算送货费、货款和押金。

            5. 用户负责填写真实订单信息、付款截图和收款 QR。骑手负责合法、安全完成配送，并提供正确收款信息。

            6. 如果骑手在取件、配送或送达过程中造成包裹或物品损坏，骑手需要按实际损失和平台规则进行赔偿。

            7. 为避免损坏，用户必须提前包好包裹。若包裹易碎、包装不完整、漏液、不安全或容易损坏，骑手有权在取件前或发现风险时取消送货。

            8. Blink 可能更新本条款，继续使用即代表接受新条款。

            客服 WhatsApp：+95 942 459 4930。
        """.trimIndent()
    }
}

private fun tabIcon(index: Int): String {
    return when (index) {
        0 -> "+"
        1 -> "≡"
        2 -> "●"
        else -> "◉"
    }
}
