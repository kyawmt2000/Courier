package com.kyawmt2000.courierrider

import android.app.Activity
import android.graphics.BitmapFactory
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.Image
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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.kyawmt2000.courierrider.model.ChatMessage
import com.kyawmt2000.courierrider.model.DeliveryOrder
import com.kyawmt2000.courierrider.model.OrderStatus
import com.kyawmt2000.courierrider.model.SettlementStatus
import com.kyawmt2000.courierrider.ui.theme.BlinkBlue
import com.kyawmt2000.courierrider.ui.theme.BlinkOrange
import com.kyawmt2000.courierrider.ui.theme.CourierRiderTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.URL
import java.time.OffsetDateTime

class MainActivity : ComponentActivity() {
    private val viewModel by viewModels<RiderViewModel>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            CourierRiderTheme {
                Surface(Modifier.fillMaxSize()) {
                    if (viewModel.isLoggedIn) {
                        RiderShell(viewModel)
                    } else {
                        LoginScreen(viewModel)
                    }
                }
            }
        }
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
private fun RiderDepositCountdown(order: DeliveryOrder, model: RiderViewModel) {
    val dueMillis = remember(order.riderDepositDueAt) {
        order.riderDepositDueAt?.let { value ->
            runCatching { OffsetDateTime.parse(value).toInstant().toEpochMilli() }.getOrNull()
        }
    } ?: return
    var nowMillis by remember { mutableStateOf(System.currentTimeMillis()) }

    LaunchedEffect(dueMillis) {
        while (true) {
            nowMillis = System.currentTimeMillis()
            delay(1_000)
        }
    }

    val remainingSeconds = ((dueMillis - nowMillis + 999) / 1_000).coerceAtLeast(0)
    val minutes = remainingSeconds / 60
    val seconds = remainingSeconds % 60
    Text(
        model.t("请在5分钟之内支付押金：%s").format("%d:%02d".format(minutes, seconds)),
        color = BlinkOrange,
        fontWeight = FontWeight.Bold
    )
}

@Composable
private fun RemoteImage(
    model: Any?,
    contentDescription: String,
    contentScale: ContentScale,
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    var bitmap by remember(model) { mutableStateOf<ImageBitmap?>(null) }

    LaunchedEffect(model) {
        bitmap = withContext(Dispatchers.IO) {
            runCatching {
                val stream = when (model) {
                    is Uri -> context.contentResolver.openInputStream(model)
                    is String -> URL(model).openStream()
                    else -> null
                }
                stream?.use { BitmapFactory.decodeStream(it)?.asImageBitmap() }
            }.getOrNull()
        }
    }

    if (bitmap != null) {
        Image(
            bitmap = bitmap!!,
            contentDescription = contentDescription,
            contentScale = contentScale,
            modifier = modifier
        )
    } else {
        Box(modifier = modifier.background(Color.LightGray.copy(alpha = 0.35f)))
    }
}

@Composable
private fun LoginScreen(model: RiderViewModel) {
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
        Text("Blink Rider", fontSize = 38.sp, fontWeight = FontWeight.Bold)
        Text("Apple / Gmail", color = Color.Gray)
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
private fun RiderShell(model: RiderViewModel) {
    var selectedTab by remember { mutableStateOf(0) }
    var chatConversationId by remember { mutableStateOf<String?>(null) }
    val tabs = listOf(model.t("接单"), model.t("订单"), model.t("聊天"), model.t("我的"))

    LaunchedEffect(selectedTab) {
        while (selectedTab == 1) {
            delay(4_000)
            model.refreshOrdersSilently()
        }
    }

    Scaffold(
        bottomBar = {
            NavigationBar {
                tabs.forEachIndexed { index, title ->
                    NavigationBarItem(
                        selected = selectedTab == index,
                        onClick = {
                            selectedTab = index
                            if (index == 0 || index == 1) model.loadOrders()
                            if (index == 2) {
                                chatConversationId = null
                                model.loadOrders()
                            }
                            if (index == 3) model.loadProfile()
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
                0 -> MatchingScreen(
                    model = model,
                    onAccepted = {
                        selectedTab = 1
                        model.loadOrders()
                    }
                )
                1 -> OrdersScreen(model)
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
                    onOpenCustomerService = {
                        chatConversationId = "main"
                        model.loadMessages("main")
                        selectedTab = 2
                    }
                )
            }
        }
    }
    if (model.user != null && !model.hasAcceptedTerms) {
        TermsDialog(model = model, required = true, onDismiss = {})
    }
}

@Composable
private fun MatchingScreen(model: RiderViewModel, onAccepted: () -> Unit) {
    var selectedOrder by remember { mutableStateOf<DeliveryOrder?>(null) }
    OrdersList(
        title = model.t("接单"),
        model = model,
        orders = model.matchingOrders,
        emptyText = model.t("暂无可接订单")
    ) { order ->
        OrderCard(
            order = order,
            model = model,
            primaryAction = model.t("接受订单"),
            onOpenDetail = { selectedOrder = order }
        ) {
            model.accept(order, onAccepted = onAccepted)
        }
    }
    selectedOrder?.let { order ->
        OrderDetailDialog(order = order, model = model, onDismiss = { selectedOrder = null })
    }
}

@Composable
private fun OrdersScreen(model: RiderViewModel) {
    var showHistory by remember { mutableStateOf(false) }
    var selectedOrder by remember { mutableStateOf<DeliveryOrder?>(null) }
    val list = if (showHistory) model.archivedOrders else model.runningOrders

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
                FilterChip(selected = !showHistory, onClick = { showHistory = false }, label = { Text(model.t("进行中")) })
                FilterChip(selected = showHistory, onClick = { showHistory = true }, label = { Text(model.t("历史")) })
            }
            StatusMessages(model)
        }
        if (list.isEmpty()) {
            item { EmptyCard(if (showHistory) model.t("暂无历史订单") else model.t("暂无进行中订单")) }
        } else {
            items(list, key = { it.id }) { order ->
                RunningOrderCard(order, model, onOpenDetail = { selectedOrder = order })
            }
        }
    }
    selectedOrder?.let { order ->
        OrderDetailDialog(order = order, model = model, onDismiss = { selectedOrder = null })
    }
}

@Composable
private fun OrdersList(
    title: String,
    model: RiderViewModel,
    orders: List<DeliveryOrder>,
    emptyText: String,
    row: @Composable (DeliveryOrder) -> Unit
) {
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(title, fontSize = 36.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                Button(onClick = { model.loadOrders() }, enabled = !model.isLoading) { Text(model.t("刷新")) }
            }
            if (model.hasRunningOrder) {
                Text(model.t("你已有进行中订单，完成后才能接受新的订单。"), color = Color.Gray, fontSize = 13.sp)
            }
            StatusMessages(model)
        }
        if (orders.isEmpty()) {
            item { EmptyCard(emptyText) }
        } else {
            items(orders, key = { it.id }) { order -> row(order) }
        }
    }
}

@Composable
private fun RunningOrderCard(order: DeliveryOrder, model: RiderViewModel, onOpenDetail: () -> Unit) {
    val context = LocalContext.current
    val action = when {
        order.statusType == OrderStatus.Completed &&
            SettlementStatus.from(order.settlementStatus) != SettlementStatus.PaidToRider &&
            SettlementStatus.from(order.settlementStatus) != SettlementStatus.Completed -> "提醒平台结算"
        order.needsDeposit -> if (order.riderDepositStatus == "pending") "等待平台确认押金" else "已转账，通知平台确认"
        order.statusType == OrderStatus.Accepted -> "前往取件"
        order.statusType == OrderStatus.PickingUp -> "开始配送"
        order.statusType == OrderStatus.Delivering -> "完成送达"
        else -> null
    }

    OrderCard(
        order = order,
        model = model,
        primaryAction = action,
        disablePrimary = order.riderDepositStatus == "pending",
        onOpenDetail = onOpenDetail
    ) {
        when {
            order.statusType == OrderStatus.Completed -> Unit
            order.needsDeposit -> model.markDepositTransferred(order)
            order.statusType == OrderStatus.Accepted -> {
                openGoogleMapsNavigation(context, order.pickupNavigationText())
                model.advance(order)
            }
            order.statusType == OrderStatus.PickingUp -> {
                openGoogleMapsNavigation(context, order.dropoffNavigationText())
                model.advance(order)
            }
            else -> model.advance(order)
        }
    }
}

@Composable
private fun OrderCard(
    order: DeliveryOrder,
    model: RiderViewModel,
    primaryAction: String? = null,
    disablePrimary: Boolean = false,
    onOpenDetail: (() -> Unit)? = null,
    onPrimary: (() -> Unit)? = null
) {
    val context = LocalContext.current
    var settlementName by remember { mutableStateOf(model.riderName) }
    var qrBytes by remember { mutableStateOf<ByteArray?>(null) }
    var showDepositQr by remember { mutableStateOf(false) }
    val qrPicker = rememberImagePicker { uri -> qrBytes = readBytes(context, uri) }
    val needsSettlement = order.statusType == OrderStatus.Completed &&
        !order.hasPaidRiderSettlement

    CardBlock(modifier = if (onOpenDetail != null) Modifier.clickable(onClick = onOpenDetail) else Modifier) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("#${order.shortCode}", fontSize = 20.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
            StatusPill(order, model)
        }
        Spacer(Modifier.height(10.dp))
        Text(order.pickupAddress, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Text(order.dropoffAddress, color = Color.Gray, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(10.dp))
        Row {
            Text(model.t(order.parcelType), color = Color.Gray)
            Spacer(Modifier.width(12.dp))
            Text("${"%.1f".format(order.weightKg)} kg", color = Color.Gray)
            Spacer(Modifier.width(12.dp))
            Text("${"%.1f".format(order.distanceKm)} km", color = Color.Gray)
            Spacer(Modifier.weight(1f))
            Text("${"%.0f".format(order.displayRiderDeliveryFee)} MMK", fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.height(8.dp))
        Text(
            "${model.t(order.paymentTitle)}  ${model.t("货值")} ${"%.0f".format(order.goodsAmount)} MMK",
            color = Color.Gray,
            fontSize = 13.sp
        )
        Text(
            "${model.t("押金")}：${model.t(order.depositTitle)}  ${model.t("结算")}：${model.t(order.settlementTitle)}",
            color = Color.Gray,
            fontSize = 13.sp
        )
        Spacer(Modifier.height(8.dp))
        FeeBreakdown(
            gross = order.grossDeliveryFee,
            platform = order.displayPlatformDeliveryFee,
            rider = order.displayRiderDeliveryFee,
            translate = model::t
        )
        order.goodsImageUrl?.let {
            Text("${model.t("商品图")}：${model.t("已选择")}", color = Color.Gray, fontSize = 13.sp)
        }
        order.paymentProofUrl?.let {
            Text("${model.t("付款截图")}：${model.t("已选择")}", color = Color.Gray, fontSize = 13.sp)
        }
        if (order.pickupLat != null && order.pickupLng != null && order.dropoffLat != null && order.dropoffLng != null) {
            Text("${model.t("坐标")}：${order.pickupLat},${order.pickupLng} → ${order.dropoffLat},${order.dropoffLng}", color = Color.Gray, fontSize = 12.sp)
        }
        if (order.note.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(order.note, color = Color.Gray, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        if (order.hasPaidRiderSettlement) {
            Spacer(Modifier.height(10.dp))
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(BlinkOrange.copy(alpha = 0.12f), RoundedCornerShape(10.dp))
                    .padding(12.dp)
            ) {
                Text(model.t("平台已转，请查收"), color = BlinkOrange, fontWeight = FontWeight.Bold)
                Text(
                    "${model.t("最终送货费")}：${"%.0f".format(order.riderSettlementBillAmount ?: order.displayRiderDeliveryFee)} MMK",
                    color = Color.DarkGray,
                    fontSize = 13.sp
                )
            }
        } else if (needsSettlement && order.hasRequestedRiderSettlement) {
            Spacer(Modifier.height(10.dp))
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(BlinkOrange.copy(alpha = 0.10f), RoundedCornerShape(10.dp))
                    .padding(12.dp)
            ) {
                Text(model.t("已提交，请稍等平台支付送货费"), color = BlinkOrange, fontWeight = FontWeight.Bold)
                Text(
                    "${model.t("最终送货费")}：${"%.0f".format(order.displayRiderDeliveryFee)} MMK",
                    color = Color.Gray,
                    fontSize = 13.sp
                )
            }
        } else if (needsSettlement) {
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
                Text(model.t("提交给平台结算"))
            }
        } else if (primaryAction != null && onPrimary != null) {
            Spacer(Modifier.height(12.dp))
            if (order.statusType != OrderStatus.Matching && order.needsDeposit) {
                RiderDepositCountdown(order = order, model = model)
                Spacer(Modifier.height(4.dp))
                Text(
                    "${model.t("平台押金（货值）")}：${"%.0f".format(order.goodsAmount)} MMK",
                    color = BlinkOrange,
                    fontWeight = FontWeight.Bold
                )
                Spacer(Modifier.height(6.dp))
                OutlinedButton(onClick = { showDepositQr = true }, modifier = Modifier.fillMaxWidth()) {
                    Text(model.t("转平台押金"))
                }
                Spacer(Modifier.height(8.dp))
            }
            Button(onClick = onPrimary, enabled = !disablePrimary && !model.isLoading, modifier = Modifier.fillMaxWidth()) {
                Text(model.t(primaryAction))
            }
        }
    }
    if (showDepositQr) {
        PlatformDepositQrDialog(order = order, model = model, onDismiss = { showDepositQr = false })
    }
}

@Composable
private fun PlatformDepositQrDialog(order: DeliveryOrder, model: RiderViewModel, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            Button(onClick = onDismiss) {
                Text(model.t("返回"))
            }
        },
        title = { Text(model.t("转平台押金")) },
        text = {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                RiderDepositCountdown(order = order, model = model)
                Text(model.t("平台押金（货值）"), color = Color.Gray, fontSize = 13.sp)
                Text("${"%.0f".format(order.goodsAmount)} MMK", fontSize = 28.sp, fontWeight = FontWeight.Bold)
                Text(
                    model.t("送达后平台会退还押金并支付送货费"),
                    color = Color.Gray,
                    fontSize = 13.sp
                )
                Text(model.t("KPay 付款二维码"), fontWeight = FontWeight.Bold)
                Image(
                    painter = painterResource(id = R.drawable.kpay_qr_code),
                    contentDescription = model.t("KPay 付款二维码"),
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.size(220.dp)
                )
            }
        }
    )
}

@Composable
private fun OrderDetailDialog(order: DeliveryOrder, model: RiderViewModel, onDismiss: () -> Unit) {
    val context = LocalContext.current
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
                DetailLine(
                    model.t("取件地址"),
                    order.pickupAddress,
                    onClick = { openGoogleMapsNavigation(context, order.pickupNavigationText()) }
                )
                DetailLine(
                    model.t("收货地址"),
                    order.dropoffAddress,
                    onClick = { openGoogleMapsNavigation(context, order.dropoffNavigationText()) }
                )
                DetailLine(model.t("付款方式"), model.t(order.paymentTitle))
                DetailLine(model.t("货物价格 MMK"), "${"%.0f".format(order.goodsAmount)} MMK")
                DetailLine(model.t("预估费用"), "${"%.0f".format(order.grossDeliveryFee)} MMK")
                DetailLine(model.t("最终送货费"), "${"%.0f".format(order.displayRiderDeliveryFee)} MMK")
                DetailLine(model.t("物品类型"), model.t(order.parcelType))
                DetailLine(model.t("付款状态"), model.t(order.depositTitle))
                if (order.note.isNotBlank()) {
                    DetailLine(model.t("备注，比如门牌号、易碎物品"), order.note)
                }
                order.goodsImageUrl?.let { url ->
                    Text(model.t("商品图"), fontWeight = FontWeight.Bold)
                    RemoteImage(
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
private fun DetailLine(label: String, value: String, onClick: (() -> Unit)? = null) {
    Column(modifier = if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier) {
        Text(label, color = Color.Gray, fontSize = 12.sp)
        Text(value, color = if (onClick != null) BlinkBlue else Color.Unspecified)
    }
}

@Composable
private fun StatusPill(order: DeliveryOrder, model: RiderViewModel) {
    val color = if (order.statusType == OrderStatus.Matching) BlinkOrange else BlinkBlue
    Text(
        model.t(order.statusType.title),
        color = color,
        modifier = Modifier
            .background(color.copy(alpha = 0.12f), RoundedCornerShape(50.dp))
            .padding(horizontal = 10.dp, vertical = 5.dp),
        fontSize = 12.sp,
        fontWeight = FontWeight.Bold
    )
}

@Composable
private fun ChatScreen(
    model: RiderViewModel,
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
    model: RiderViewModel,
    onOpenConversation: (String) -> Unit
) {
    val orderConversations = (model.runningOrders + model.archivedOrders).take(20)

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        item {
            Text(model.t("聊天"), fontSize = 36.sp, fontWeight = FontWeight.Bold)
            Text(model.t("聊天提示"), color = Color.Gray, fontSize = 13.sp)
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
                    subtitle = "${model.t(order.statusType.title)}  ${order.pickupAddress}",
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
    model: RiderViewModel,
    conversationId: String,
    onBackToList: () -> Unit
) {
    val context = LocalContext.current
    var draft by remember { mutableStateOf("") }
    var imageBytes by remember { mutableStateOf<ByteArray?>(null) }
    val imagePicker = rememberImagePicker { uri -> imageBytes = readBytes(context, uri) }
    val order = (model.runningOrders + model.archivedOrders)
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
            items(model.chatMessages, key = { it.id }) { message -> ChatBubble(message, model) }
        }
        item {
            CardBlock {
                OutlinedTextField(draft, { draft = it }, label = { Text(model.t("输入消息")) }, modifier = Modifier.fillMaxWidth())
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
private fun ChatBubble(message: ChatMessage, model: RiderViewModel) {
    val isMine = message.senderType == "rider"
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
    model: RiderViewModel,
    onOpenCustomerService: () -> Unit
) {
    val context = LocalContext.current
    var nickname by remember(model.user?.nickname) { mutableStateOf(model.user?.nickname ?: model.riderName) }
    var avatarUri by remember { mutableStateOf<Uri?>(null) }
    var avatarBytes by remember { mutableStateOf<ByteArray?>(null) }
    var paymentQrBytes by remember { mutableStateOf<ByteArray?>(null) }
    var showTerms by remember { mutableStateOf(false) }
    var showDeleteAccountConfirmation by remember { mutableStateOf(false) }
    var showAllSettledOrders by remember { mutableStateOf(false) }
    val avatarPicker = rememberImagePicker { uri ->
        avatarUri = uri
        avatarBytes = readBytes(context, uri)
    }
    val qrPicker = rememberImagePicker { uri -> paymentQrBytes = readBytes(context, uri) }
    val billedOrders = model.orders
        .filter { it.hasPaidRiderSettlement }
        .sortedByDescending { it.riderSettlementBillCreatedAt ?: it.riderSettlementPaidAt ?: it.createdAt }
    var selectedSettledOrder by remember { mutableStateOf<DeliveryOrder?>(null) }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item { Text(model.t("我的账号"), fontSize = 36.sp, fontWeight = FontWeight.Bold) }
        item {
            CardBlock {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    val avatarModel = avatarUri ?: model.user?.avatarUrl
                    if (avatarModel != null) {
                        RemoteImage(
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
                                nickname.trim().take(1).ifBlank { "R" }.uppercase(),
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
                OutlinedButton(onClick = { qrPicker.launch("image/*") }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (model.user?.paymentQrUrl == null && paymentQrBytes == null) model.t("上传我的收款码") else model.t("更换我的收款码"))
                }
                Button(
                    onClick = { model.updateProfile(nickname, avatarBytes, paymentQrBytes) },
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
                Text(model.t("我的收款码"), fontWeight = FontWeight.Bold)
                Spacer(Modifier.height(8.dp))
                if (model.user?.paymentQrUrl == null) {
                    Text(model.t("未上传"), color = Color.Gray)
                } else {
                    RemoteImage(
                        model = model.user?.paymentQrUrl,
                        contentDescription = model.t("我的收款码"),
                        contentScale = ContentScale.Fit,
                        modifier = Modifier
                            .size(220.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .background(Color.White)
                            .padding(8.dp)
                            .align(Alignment.CenterHorizontally)
                    )
                }
            }
        }
        if (billedOrders.isNotEmpty()) {
            item {
                CardBlock {
                    Text(model.t("已结算订单"), fontWeight = FontWeight.Bold)
                    billedOrders.firstOrNull()?.let { order ->
                        SettledOrderRow(order = order, model = model) {
                            selectedSettledOrder = order
                        }
                    }
                    if (billedOrders.size > 1) {
                        OutlinedButton(
                            onClick = { showAllSettledOrders = true },
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text(model.t("所有订单"))
                        }
                    }
                }
            }
        }
        item {
            CardBlock {
                Text(model.t("语言设置"), fontWeight = FontWeight.Bold)
                AppLanguage.entries.forEach { language ->
                    OutlinedButton(
                        onClick = { model.chooseLanguage(language) },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(if (model.language == language) "${language.title} (${model.t("已选择")})" else language.title)
                    }
                    Spacer(Modifier.height(6.dp))
                }
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
                Button(onClick = onOpenCustomerService, modifier = Modifier.fillMaxWidth()) {
                    Text(model.t("客服中心"))
                }
                Spacer(Modifier.height(8.dp))
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
    if (showAllSettledOrders) {
        AlertDialog(
            onDismissRequest = { showAllSettledOrders = false },
            confirmButton = {
                Button(onClick = { showAllSettledOrders = false }) {
                    Text(model.t("返回"))
                }
            },
            title = { Text(model.t("所有订单")) },
            text = {
                LazyColumn(
                    modifier = Modifier.heightIn(max = 520.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    items(billedOrders) { order ->
                        SettledOrderRow(order = order, model = model) {
                            showAllSettledOrders = false
                            selectedSettledOrder = order
                        }
                    }
                }
            }
        )
    }
    selectedSettledOrder?.let { order ->
        OrderDetailDialog(order = order, model = model, onDismiss = { selectedSettledOrder = null })
    }
}

@Composable
private fun SettledOrderRow(order: DeliveryOrder, model: RiderViewModel, onClick: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(vertical = 10.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("#${order.shortCode}", fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
            Text("${"%.0f".format(order.riderSettlementBillAmount ?: order.displayRiderDeliveryFee)} MMK", color = BlinkOrange)
        }
        Text(model.t(order.riderSettlementBillTitle ?: "送货费已结算"), color = Color.Gray, fontSize = 13.sp)
        order.riderSettlementBillCreatedAt?.let {
            Text(it, color = Color.Gray, fontSize = 12.sp)
        }
    }
}

@Composable
private fun TermsDialog(model: RiderViewModel, required: Boolean, onDismiss: () -> Unit) {
    var checked by remember(model.hasAcceptedTerms) { mutableStateOf(model.hasAcceptedTerms) }
    AlertDialog(
        onDismissRequest = { if (!required) onDismiss() },
        confirmButton = {
            Button(
                onClick = {
                    model.acceptTerms()
                    onDismiss()
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
                StatusMessages(model)
            }
        }
    )
}
@Composable
private fun StatusMessages(model: RiderViewModel) {
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

private fun openGoogleMapsNavigation(context: android.content.Context, destination: String) {
    val navigationUri = Uri.parse("google.navigation:q=${Uri.encode(destination)}&mode=d")
    val navigationIntent = Intent(Intent.ACTION_VIEW, navigationUri).apply {
        setPackage("com.google.android.apps.maps")
    }
    val fallbackIntent = Intent(
        Intent.ACTION_VIEW,
        Uri.parse("https://www.google.com/maps/dir/?api=1&destination=${Uri.encode(destination)}&travelmode=driving")
    )
    val intent = if (navigationIntent.resolveActivity(context.packageManager) != null) {
        navigationIntent
    } else {
        fallbackIntent
    }
    context.startActivity(intent)
}

private fun DeliveryOrder.pickupNavigationText(): String {
    return coordinateText(pickupLat, pickupLng) ?: googleMapsLocationText(pickupAddress)
}

private fun DeliveryOrder.dropoffNavigationText(): String {
    return coordinateText(dropoffLat, dropoffLng) ?: googleMapsLocationText(dropoffAddress)
}

private fun coordinateText(lat: Double?, lng: Double?): String? {
    return if (lat != null && lng != null) "$lat,$lng" else null
}

private fun googleMapsLocationText(address: String): String {
    val marker = "Google Map Location:"
    val markerIndex = address.indexOf(marker, ignoreCase = true)
    if (markerIndex >= 0) {
        return address.substring(markerIndex + marker.length).trim()
    }
    return address
}

private fun tabIcon(index: Int): String {
    return when (index) {
        0 -> "+"
        1 -> "≡"
        2 -> "●"
        else -> "◉"
    }
}
